"""记忆系统：唯一的“目录文件” + 多个“目的文件”。

文件格式设计为人类可直接阅读和编辑的 UTF-8 Markdown：

- memory/目录.md        唯一的目录文件
- memory/目的/<id>-<主题>.md  每个目的文件对应一次具体对话/工作

目录文件中每条记忆：
    <!-- memory:start -->
    ```yaml
    id: ...
    time: ...
    topic: ...
    type: chat / work / browser / config / note
    weight: 0.80          # 当前静态权重
    importance: 0.85      # 重要性判定分数
    purpose: 目的/xxx.md  # 目的文件相对路径
    tags: [...]
    ```
    ### 主题
    **时间**：...
    **权重**：...
    **目的文件**：[xxx](目的/xxx.md)
    人类可读摘要
    <!-- memory:end -->

大模型聊天时只读目录文件的摘要；只有当用户聊到相关内容时，才按 purpose
地址读取对应目的文件。任何越出目录文件记录的路径都不会被读取。
"""
from __future__ import annotations

import ast
import datetime as dt
import math
import random
import re
import threading
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from ..config import DEFAULT_CONFIG, DIRECTORY_FILE, PURPOSE_DIR

_ENTRY_RE = re.compile(r"^[ 	]*<!--\s*memory:start\s*-->(.*?)^[ 	]*<!--\s*memory:end\s*-->", re.S | re.M)
_YAML_BLOCK_RE = re.compile(r"```ya?ml\s*\n(.*?)```", re.S)
_HEADING_RE = re.compile(r"^#{1,6}\s*(.+?)\s*#*\s*$", re.M)

_BLOCKED_PARTS = ("..", "/", "\\", "\x00")


def now_iso() -> str:
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


def make_id(when: dt.datetime | None = None) -> str:
    when = when or dt.datetime.now().astimezone()
    return f"M{when:%Y%m%d-%H%M%S}-{random.randrange(0x1000):03x}".upper()


def slugify(text: str, max_len: int = 24) -> str:
    text = unicodedata.normalize("NFKC", text or "").strip()
    text = re.sub(r"[^\w\u4e00-\u9fff\-]+", "-", text, flags=re.UNICODE)
    text = re.sub(r"-{2,}-", "-", text).strip("-")
    return (text[:max_len] or "memory")


def time_decay(age_days: float, half_life_days: float = 30.0) -> float:
    """指数时间衰减：新事件接近 1，旧事件逐步下降但不归零。"""
    if age_days <= 0:
        return 1.0
    half_life = max(float(half_life_days), 0.5)
    return max(0.05, math.exp(-math.log(2) * age_days / half_life))


def parse_scalar(raw: str) -> Any:
    raw = raw.strip()
    if not raw:
        return ""
    if raw in ("null", "None", "~"):
        return None
    if raw.startswith("[") and raw.endswith("]"):
        inner = raw[1:-1].strip()
        if not inner:
            return []
        return [parse_scalar(part) for part in inner.split(",")]
    if raw.startswith(("{", '"', "'")):
        try:
            return ast.literal_eval(raw)
        except Exception:
            return raw.strip("\"'")
    try:
        if raw.lower() in ("true", "false"):
            return raw.lower() == "true"
        if "." in raw or "e" in raw.lower():
            return float(raw)
        return int(raw)
    except Exception:
        return raw


def parse_yaml_block(text: str) -> dict[str, Any]:
    match = _YAML_BLOCK_RE.search(text)
    data: dict[str, Any] = {}
    if not match:
        return data
    for line in match.group(1).splitlines():
        line = line.rstrip()
        if not line or line.lstrip().startswith("#") or ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip()
        if key:
            data[key] = parse_scalar(value)
    return data


@dataclass
class MemoryEntry:
    id: str
    time: str
    topic: str
    type: str = "note"
    purpose: str = ""
    weight: float = 0.5
    base_weight: float = 0.5
    importance: float = 0.5
    summary: str = ""
    tags: list[str] = field(default_factory=list)
    source: str = ""
    keywords: list[str] = field(default_factory=list)
    status: str = "active"
    reasons: list[str] = field(default_factory=list)
    raw_block: str = ""
    parsed_ok: bool = True
    parse_error: str = ""

    @property
    def occurred_at(self) -> dt.datetime | None:
        try:
            return dt.datetime.fromisoformat(self.time)
        except Exception:
            return None

    @property
    def age_days(self) -> float:
        when = self.occurred_at
        if not when:
            return 999999.0
        return max(0.0, (dt.datetime.now().astimezone() - when).total_seconds() / 86400.0)

    def effective_weight(self, half_life_days: float = 30.0) -> float:
        return max(0.0, min(1.0, float(self.weight or 0) * time_decay(self.age_days, half_life_days)))

    def to_llm_line(self, max_summary: int = 120) -> str:
        summary = re.sub(r"\s+", " ", self.summary or "").strip()
        if len(summary) > max_summary:
            summary = summary[:max_summary] + "…"
        tags = ",".join(self.tags or [])
        return (
            f"[{self.id}] {self.time} | 主题：{self.topic} | 类型：{self.type} | "
            f"权重：{self.weight:.2f} | 目的文件：{self.purpose or '无'} | 标签：{tags or '无'}\n"
            f"    摘要：{summary or '无'}"
        )


class MemoryManager:
    """读写目录文件和目的文件。所有方法线程安全。"""

    def __init__(self, root: Path | str | None = None, config: Any = None):
        cfg = config.as_dict() if config is not None else DEFAULT_CONFIG
        root = Path(root) if root else Path(cfg.get("memory_root", None) or DIRECTORY_FILE.parent)
        self.root = Path(root)
        self.directory_file = self.root / DIRECTORY_FILE.name
        self.purpose_dir = self.root / PURPOSE_DIR.name
        self.encoding = str(cfg.get("memory", {}).get("encoding", "utf-8"))
        self._lock = threading.RLock()
        self._threshold = float(cfg.get("memory", {}).get("default_min_importance", 0.45))
        self._init_files()

    def _init_files(self) -> None:
        with self._lock:
            self.root.mkdir(parents=True, exist_ok=True)
            self.purpose_dir.mkdir(parents=True, exist_ok=True)
            if not self.directory_file.exists():
                self.directory_file.write_text(_INITIAL_DIRECTORY, encoding=self.encoding)
            welcome = self.purpose_dir / "WELCOME-0000-记忆系统已初始化.md"
            if not welcome.exists():
                welcome.write_text(
                    "---\n"
                    "id: WELCOME-0000\n"
                    "time: 2025-01-01T00:00:00+00:00\n"
                    "topic: 记忆系统已初始化\n"
                    "type: purpose\n"
                    "---\n\n"
                    "# 记忆系统已初始化\n\n"
                    "这是系统占位目的文件，用于演示目录地址到目的文件的映射。\n",
                    encoding=self.encoding,
                )

    # ---------- 读取目录文件 ----------
    def read_directory_text(self) -> str:
        with self._lock:
            try:
                return self.directory_file.read_text(encoding=self.encoding)
            except Exception:
                return _INITIAL_DIRECTORY

    def list_entries(self, include_invalid: bool = True) -> list[MemoryEntry]:
        text = self.read_directory_text()
        entries: list[MemoryEntry] = []
        for block in _ENTRY_RE.finditer(text):
            raw = block.group(1)
            data = parse_yaml_block(raw)
            try:
                entry = MemoryEntry(
                    id=str(data.get("id") or "LEGACY"),
                    time=str(data.get("time") or "1970-01-01T00:00:00+00:00"),
                    topic=str(data.get("topic") or "未命名记忆"),
                    type=str(data.get("type") or "note"),
                    purpose=str(data.get("purpose") or ""),
                    weight=float(data.get("weight", 0.5)),
                    base_weight=float(data.get("base_weight", float(data.get("weight", 0.5)))),
                    importance=float(data.get("importance", 0.5)),
                    tags=list(data.get("tags") or []),
                    source=str(data.get("source") or ""),
                    keywords=list(data.get("keywords") or []),
                    status=str(data.get("status") or "active"),
                    reasons=list(data.get("reasons") or []),
                    raw_block=raw,
                )
                entry.summary = _extract_human_summary(raw)
                if entry.id == "WELCOME-0000":
                    entry.summary = "这是一条系统占位记忆，用于演示目录与目的文件的对应关系。"
                entries.append(entry)
            except Exception as exc:  # 用户手改导致单条损坏时不影响其余条目
                if include_invalid:
                    entries.append(MemoryEntry(
                        id="INVALID", time="", topic="（无法解析的记忆块）",
                        purpose="", summary=raw[:200], parsed_ok=False, parse_error=str(exc),
                        raw_block=raw,
                    ))
        return entries

    def sorted_entries(self, limit: int | None = None, half_life_days: float = 30.0) -> list[MemoryEntry]:
        entries = [e for e in self.list_entries() if e.parsed_ok and e.status != "archived"]
        entries.sort(key=lambda e: e.effective_weight(half_life_days), reverse=True)
        if limit is not None:
            entries = entries[: max(1, limit)]
        return entries

    def get_entry(self, entry_id: str) -> MemoryEntry | None:
        for e in self.list_entries():
            if e.id.lower() == str(entry_id).lower():
                return e
        return None

    # ---------- 目的文件 ----------
    def resolve_purpose_path(self, entry: MemoryEntry) -> Path | None:
        """只允许读取目录文件中记录过的目的文件路径，防目录穿越。"""
        if not entry or not entry.purpose:
            return None
        raw = str(entry.purpose).strip()
        if "\x00" in raw:
            return None
        parts = [p for p in raw.replace("\\", "/").split("/") if p not in ("", ".")]
        if any(p == ".." for p in parts):
            return None
        path = (self.root / raw).resolve()
        try:
            path.relative_to(self.root.resolve())
        except ValueError:
            return None
        return path if path.is_file() else None

    def read_purpose(self, entry: MemoryEntry, max_chars: int = 14000) -> str:
        path = self.resolve_purpose_path(entry)
        if path is None:
            return ""
        with self._lock:
            try:
                text = path.read_text(encoding=self.encoding)
                return text[: int(max_chars)]
            except Exception:
                return ""

    def purpose_text_for_llm(self, entry: MemoryEntry, max_chars: int = 14000) -> str:
        content = self.read_purpose(entry, max_chars)
        if not content:
            return f"（目的文件不存在或不可读：{entry.purpose}）"
        return f"===== 目的文件 {entry.id}：{entry.purpose} =====\n{content}"

    # ---------- 给大模型的目录摘要（只含目录，绝不含目的文件正文） ----------
    def directory_digest_text(self, limit: int = 120, half_life_days: float = 30.0) -> str:
        entries = self.sorted_entries(limit=limit, half_life_days=half_life_days)
        lines = ["# 目录文件摘要（只读目录，不要臆造未列出的目的文件）", ""]
        for i, e in enumerate(entries, 1):
            lines.append(f"{i}. {e.to_llm_line()}")
            lines.append("")
        return "\n".join(lines)

    # ---------- 写入 ----------
    def write_purpose_file(self, entry_id: str, topic: str, body_markdown: str,
                           extra_meta: dict[str, Any] | None = None) -> Path:
        slug = slugify(topic)
        filename = f"{entry_id}-{slug}.md"
        path = self.purpose_dir / filename
        with self._lock:
            meta: dict[str, Any] = {
                "id": entry_id,
                "time": now_iso(),
                "topic": topic,
                "type": "purpose",
            }
            if extra_meta:
                meta.update(extra_meta)
            front = ["---"] + [f"{k}: {_dump_scalar(v)}" for k, v in meta.items()] + ["---"]
            path.write_text(
                "\n".join(front) + "\n\n" + body_markdown.rstrip() + "\n",
                encoding=self.encoding,
            )
        return path

    def add_entry(self, topic: str, purpose_body: str, *, entry_type: str = "note",
                  occurred_at: dt.datetime | None = None, purpose_rel: str | None = None,
                  tags: list[str] | None = None, keywords: list[str] | None = None,
                  importance: float = 0.6, weight: float = 0.6, summary: str = "",
                  source: str = "", reasons: list[str] | None = None,
                  extra_meta: dict[str, Any] | None = None,
                  status: str = "active") -> tuple[MemoryEntry, Path]:
        with self._lock:
            occurred_at = occurred_at or dt.datetime.now().astimezone()
            entry_id = make_id(occurred_at)
            purpose_rel = purpose_rel or f"目的/{entry_id}-{slugify(topic)}.md"
            path = self.write_purpose_file(
                entry_id, topic, purpose_body,
                extra_meta={
                    "occurred_at": occurred_at.isoformat(timespec="seconds"),
                    "type": entry_type,
                    "importance": round(float(importance), 4),
                    "weight": round(float(weight), 4),
                    "source": source,
                    "tags": tags or [],
                    "keywords": keywords or [],
                    "reasons": reasons or [],
                    **(extra_meta or {}),
                },
            )
            rel = path.relative_to(self.root).as_posix()
            entry = MemoryEntry(
                id=entry_id, time=occurred_at.isoformat(timespec="seconds"),
                topic=topic, type=entry_type, purpose=rel,
                weight=float(weight), base_weight=float(weight), importance=float(importance),
                # 目录文件只保留“大致主题”，具体内容必须留在目的文件
                summary=summary or topic,
                tags=list(tags or []), source=source, keywords=list(keywords or []),
                status=status, reasons=list(reasons or []),
            )
            self._append_directory_entry(entry)
            return entry, path

    def _append_directory_entry(self, entry: MemoryEntry) -> None:
        block = _render_entry_block(entry)
        with self._lock:
            text = self.read_directory_text()
            if entry.id.lower() in text.lower():
                return
            text = text.rstrip() + "\n\n" + block.rstrip() + "\n"
            tmp = self.directory_file.with_suffix(".tmp")
            tmp.write_text(text, encoding=self.encoding)
            tmp.replace(self.directory_file)

    def update_entry_field(self, entry_id: str, field: str, value: Any) -> bool:
        """最小化替换某条记忆的 YAML 字段，尽量保留用户手工编辑的其他内容。"""
        if field not in {"weight", "importance", "status", "summary", "topic", "tags", "purpose", "type"}:
            return False
        with self._lock:
            text = self.read_directory_text()
            changed = False
            for block in _ENTRY_RE.finditer(text):
                data = parse_yaml_block(block.group(1))
                if str(data.get("id", "")).lower() != entry_id.lower():
                    continue
                new_yaml = _set_yaml_field(block.group(1), field, value)
                new_raw = f"<!-- memory:start -->{new_yaml}<!-- memory:end -->"
                text = text[:block.start()] + new_raw + text[block.end():]
                changed = True
                break
            if changed:
                tmp = self.directory_file.with_suffix(".tmp")
                tmp.write_text(text, encoding=self.encoding)
                tmp.replace(self.directory_file)
            return changed

    # ---------- 高层记录方法 ----------
    def record_chat(self, topic: str, messages: Iterable[dict[str, str]], *,
                    importance: float | None = None, weight: float | None = None,
                    tags: list[str] | None = None, keywords: list[str] | None = None,
                    reasons: list[str] | None = None, source: str = "chat",
                    user_name: str = "用户", assistant_name: str = "DS娘") -> MemoryEntry | None:
        """把一次完整对话写成一个目的文件，并视重要度决定是否加入目录。

        真正调用前应由 ImportanceJudge 判定；此处 importance 为 None 时
        使用保守默认值 0.55，低于阈值的对话不会进入目录文件。
        """
        importance = 0.55 if importance is None else float(importance)
        weight = importance if weight is None else float(weight)
        body = _render_chat_body(topic, messages, user_name, assistant_name)
        return self._store_if_allowed(
            topic, body, importance=importance, weight=weight, entry_type="chat",
            tags=tags or ["对话"], keywords=keywords or [], reasons=reasons or [], source=source,
        )

    def record_work(self, topic: str, body_markdown: str, *, importance: float,
                    weight: float, tags: list[str] | None = None, keywords: list[str] | None = None,
                    reasons: list[str] | None = None, source: str = "work") -> MemoryEntry | None:
        return self._store_if_allowed(
            topic, body_markdown, importance=importance, weight=weight, entry_type="work",
            tags=tags or ["工作"], keywords=keywords or [], reasons=reasons or [], source=source,
        )

    def record_browser(self, topic: str, body_markdown: str, *, importance: float,
                       weight: float, tags: list[str] | None = None, keywords: list[str] | None = None,
                       reasons: list[str] | None = None, source: str = "browser") -> MemoryEntry | None:
        return self._store_if_allowed(
            topic, body_markdown, importance=importance, weight=weight, entry_type="browser",
            tags=tags or ["浏览"], keywords=keywords or [], reasons=reasons or [], source=source,
        )

    def record_config_change(self, changes: list[dict[str, Any]], user_note: str = "") -> MemoryEntry:
        """配置的任何改动都必须进入目录文件和目的文件（API Key 等秘密自动脱敏）。"""
        safe_changes = [_mask_secret_change(ch) for ch in changes]
        body = _render_config_body(safe_changes, user_note)
        return self._store_if_allowed(
            "助手配置调整", body, importance=0.92, weight=0.9, entry_type="config",
            tags=["配置"], keywords=["配置", "参数调整"], reasons=["用户主动修改配置，强制记录"],
            source="config", force=True,
        )

    def _store_if_allowed(self, topic, body, *, importance, weight, entry_type,
                          tags, keywords, reasons, source, force=False) -> MemoryEntry | None:
        if force or importance >= self._threshold:
            entry, _ = self.add_entry(
                topic, body, entry_type=entry_type, importance=importance, weight=weight,
                tags=tags, keywords=keywords, reasons=reasons, source=source,
            )
            return entry
        return None

    def set_threshold(self, value: float) -> None:
        self._threshold = float(value)


_INITIAL_DIRECTORY = """# 记忆目录（目录文件，全项目唯一）

> 本文件是 AI 助手唯一的“目录文件”。助手日常只阅读本文件；只有当聊天内容涉及某条记忆时，
> 才会按照该条的“目的文件”地址去 `memory/目的/` 打开具体内容。
>
> 你可以直接用编辑器查看、修改本文件。每条记忆以 `<!-- memory:start -->` 和
> `<!-- memory:end -->` 包裹，中间为 YAML 元数据与可读摘要。修改时请保留这两个标记。
>
> 字段说明：
> - `id`：记忆编号（也对应目的文件名）
> - `time`：发生时间
> - `topic`：大致主题
> - `type`：chat 对话 / work 工作 / browser 浏览 / config 配置修改 / note 笔记
> - `weight`：当前权重（0~1，已综合重要性、来源、时间等因素）
> - `importance`：重要性判定分
> - `purpose`：目的文件相对本目录的路径

<!-- memory:start -->
```yaml
id: WELCOME-0000
time: 2025-01-01T00:00:00+00:00
topic: 记忆系统已初始化
type: note
weight: 0.05
base_weight: 0.05
importance: 0.05
purpose: 目的/WELCOME-0000-记忆系统已初始化.md
tags: [系统]
source: system
keywords: [目录文件, 目的文件]
status: active
reasons: [初始化占位]
```
### 记忆系统已初始化

**时间**：2025-01-01T00:00:00+00:00
**权重**：0.05
**目的文件**：[目的/WELCOME-0000-记忆系统已初始化.md](目的/WELCOME-0000-记忆系统已初始化.md)

这是一条系统占位记忆。真正的工作、聊天记录会追加在下方。
<!-- memory:end -->
"""


def _extract_human_summary(raw: str) -> str:
    text = _YAML_BLOCK_RE.sub("", raw)
    text = _HEADING_RE.sub("", text)
    text = re.sub(r"\*\*[^*]+\*\*：?", "", text)
    text = re.sub(r"[#*_>`|]", "", text)
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    return "\n".join(lines)[:300]


def _dump_scalar(value: Any) -> str:
    if isinstance(value, str):
        escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
        return f'"{escaped}"'
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_dump_scalar(v) for v in value) + "]"
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "null"
    return str(value)


def _render_entry_block(entry: MemoryEntry) -> str:
    yaml_lines = [
        "```yaml",
        f"id: {entry.id}",
        f"time: {entry.time}",
        f"topic: {_dump_scalar(entry.topic)}",
        f"type: {entry.type}",
        f"weight: {float(entry.weight):.4f}",
        f"base_weight: {float(entry.base_weight):.4f}",
        f"importance: {float(entry.importance):.4f}",
        f"purpose: {entry.purpose}",
        f"tags: {_dump_scalar(entry.tags)}",
        f"source: {_dump_scalar(entry.source)}",
        f"keywords: {_dump_scalar(entry.keywords)}",
        f"status: {entry.status}",
        f"reasons: {_dump_scalar(entry.reasons)}",
        "```",
    ]
    summary = (entry.summary or "").strip() or "（暂无摘要）"
    return "\n".join([
        "<!-- memory:start -->",
        *yaml_lines,
        "",
        f"### {entry.topic}",
        "",
        f"**时间**：{entry.time}",
        f"**权重**：{float(entry.weight):.4f}",
        f"**重要性**：{float(entry.importance):.4f}",
        f"**类型**：{entry.type}",
        f"**目的文件**：[{entry.purpose}]({entry.purpose})",
        "",
        summary.strip(),
        "<!-- memory:end -->",
    ])


def _set_yaml_field(raw: str, field: str, value: Any) -> str:
    pattern = re.compile(rf"^(\s*{re.escape(field)}\s*:\s*)(.*)$", re.M)
    rendered = _dump_scalar(value)
    if pattern.search(raw):
        return pattern.sub(lambda m: m.group(1) + rendered, raw, count=1)
    # 字段不存在时插到 yaml 代码块首行之后
    first = raw.find("\n")
    if first != -1:
        return raw[:first + 1] + f"{field}: {rendered}\n" + raw[first + 1:]
    return f"{field}: {rendered}\n" + raw


def _summarize(body: str, max_len: int = 160) -> str:
    body = re.sub(r"[#>*_`|]", " ", body or "")
    body = re.sub(r"\s+", " ", body).strip()
    return body[:max_len]


def _render_chat_body(topic: str, messages: Iterable[dict[str, str]],
                      user_name: str = "用户", assistant_name: str = "DS娘") -> str:
    lines = [f"# {topic}", "", "## 对话内容", ""]
    for msg in messages:
        role = str(msg.get("role", "user")).lower()
        content = str(msg.get("content", "")).strip()
        if role in ("user", "human"):
            lines.append(f"**{user_name}**：{content}")
        elif role in ("assistant", "ai"):
            lines.append(f"**{assistant_name}**：{content}")
        else:
            lines.append(f"**{role}**：{content}")
        lines.append("")
    lines += ["## 备注", "", "本文件由助手在对话结束后自动生成，可手动编辑。", ""]
    return "\n".join(lines)


def _mask_secret_change(change: dict[str, Any]) -> dict[str, Any]:
    ch = dict(change)
    path = str(ch.get("path", "")).lower()
    if "api_key" in path or "password" in path or "secret" in path:
        for key in ("old", "new"):
            val = str(ch.get(key, ""))
            ch[key] = (val[:3] + "***" + val[-3:]) if len(val) > 8 else "***"
    return ch


def _render_config_body(changes: list[dict[str, Any]], user_note: str = "") -> str:
    lines = ["# 助手配置调整记录", "", "## 变更明细", "", "| 参数 | 旧值 | 新值 |", "|---|---|---|"]
    for ch in changes:
        old = str(ch.get("old")).replace("|", "\\|")
        new = str(ch.get("new")).replace("|", "\\|")
        lines.append(f"| `{ch.get('path')}` | {old} | {new} |")
    if user_note:
        lines += ["", "## 用户备注", "", user_note, ""]
    lines += ["", "## 说明", "", "用户可自行调整助手参数；每一次改动都按规则记录。", ""]
    return "\n".join(lines)


__all__ = [
    "MemoryEntry", "MemoryManager", "make_id", "slugify", "time_decay",
    "now_iso", "parse_yaml_block",
]
