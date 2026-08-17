"""应用装配层：把配置、记忆、判定器、DeepSeek、活动监视器组合起来。

GUI 和 CLI 都从这里获取 AppContext，避免各入口重复初始化。
"""
from __future__ import annotations

import datetime as dt
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import DATA_DIR

from .brain import DeepSeekError, DeepSeekHarness
from .brain.chat_engine import ChatEngine
from .config import APP_DIR, ConfigManager
from .judge import ActivityEvent, ImportanceJudge, JudgeResult
from .memory import MemoryManager
from .monitor import ActivityMonitor, WorkSession


@dataclass
class AppContext:
    config: ConfigManager
    memory: MemoryManager
    judge: ImportanceJudge
    harness: DeepSeekHarness
    engine: ChatEngine
    monitor: ActivityMonitor
    recent_events: list[dict[str, Any]] = field(default_factory=list)


def build_context() -> AppContext:
    config = ConfigManager()
    memory = MemoryManager(config=config)
    memory.set_threshold(float(config.get("memory.default_min_importance", 0.45)))
    harness = DeepSeekHarness(config)
    judge = ImportanceJudge(config)
    monitor = ActivityMonitor(config)
    engine = ChatEngine(config, memory, judge, harness, monitor)
    ctx = AppContext(config, memory, judge, harness, engine, monitor)
    monitor.on_session = lambda session: record_work_session(ctx, session)
    sync_external_config_changes(ctx)
    return ctx


# 记录“上一次见到的 config.json”，用于检测用户绕过设置界面直接编辑的情况
_CONFIG_MARK_FILE = DATA_DIR / "config_mark.json"


def _mask_secret(path: str, value: Any) -> Any:
    if "api_key" in path or "password" in path or "secret" in path:
        if isinstance(value, str) and value:
            return value[:3] + "***" + value[-3:] if len(value) > 8 else "***"
        return "***"
    return value


def _flatten(data: dict[str, Any], prefix: str = "") -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in data.items():
        path = f"{prefix}.{key}" if prefix else key
        if isinstance(value, dict):
            out.update(_flatten(value, path))
        else:
            out[path] = value
    return out


def _flatten_masked(data: dict[str, Any]) -> dict[str, Any]:
    """指纹用于比较配置是否变化；API Key 等秘密字段只保存脱敏值。"""
    flat = _flatten(data)
    return {k: _mask_secret(k, v) for k, v in flat.items()}


def _diff_configs(old_flat: dict[str, Any], new_data: dict[str, Any]) -> list[dict[str, Any]]:
    # 两边都使用脱敏后的扁平值比较，避免 API Key 每次启动被误判为“改动”
    new_flat = _flatten_masked(new_data)
    old_flat = {k: _mask_secret(k, v) for k, v in old_flat.items()}
    changes: list[dict[str, Any]] = []
    for path in sorted(set(old_flat) | set(new_flat)):
        old = old_flat.get(path, "<不存在>")
        new = new_flat.get(path, "<不存在>")
        if old != new:
            changes.append({"path": path, "old": old, "new": new})
    return changes


def mark_config_recorded(ctx: AppContext) -> None:
    """在已把配置改动写入记忆后调用，更新指纹。"""
    snapshot = ctx.config.as_dict()
    fp = _flatten_masked(snapshot)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    _CONFIG_MARK_FILE.write_text(
        json.dumps({"fingerprint": fp, "saved_at": dt.datetime.now().astimezone().isoformat(timespec="seconds")},
                   ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def sync_external_config_changes(ctx: AppContext) -> list[dict[str, Any]]:
    """检测直接编辑 config.json 的改动；发现后也强制写入双文件记忆。"""
    snapshot = ctx.config.as_dict()
    fp = _flatten_masked(snapshot)
    changes: list[dict[str, Any]] = []
    if _CONFIG_MARK_FILE.exists():
        try:
            mark = json.loads(_CONFIG_MARK_FILE.read_text(encoding="utf-8"))
            old = mark.get("fingerprint", {}) if isinstance(mark, dict) else {}
            changes = _diff_configs(old, snapshot)
        except Exception:
            changes = []
    if changes:
        masked = [
            {"path": ch["path"], "old": _mask_secret(ch["path"], ch["old"]),
             "new": _mask_secret(ch["path"], ch["new"])}
            for ch in changes
        ]
        ctx.memory.record_config_change(masked, "检测到用户直接编辑了 config.json，自动补记。")
    mark_config_recorded(ctx)
    return changes


def record_work_session(ctx: AppContext, session: WorkSession) -> JudgeResult:
    """工作会话结束后，由独立判定器决定是否写入目录/目的文件。"""
    event = ActivityEvent(
        topic=session.topic(),
        occurred_at=session.started_at or dt.datetime.now().astimezone(),
        source="work",
        detail=_work_detail(session),
        duration_seconds=session.duration_seconds,
        message_count=1,
        tags=["工作", session.process_name] if session.process_name else ["工作"],
        keywords=[session.process_name, "工作"],
        process=session.process_name,
        window_title=session.title,
    )
    ctx.judge.set_directory_entries(ctx.memory.list_entries())
    result = ctx.judge.judge(event)
    ctx.recent_events.append({
        "time": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "type": "work_session",
        "topic": event.topic,
        "judge": result.to_dict(),
    })
    if result.should_store:
        ctx.memory.record_work(
            event.topic,
            _render_work_body(session),
            importance=result.importance,
            weight=result.weight,
            tags=event.tags,
            keywords=event.keywords,
            reasons=result.reasons,
            source="work",
        )
    return result


def _work_detail(session: WorkSession) -> str:
    lines = [
        f"进程：{session.process_name}",
        f"窗口标题：{session.title}",
        f"持续秒数：{session.duration_seconds:.0f}",
    ]
    if session.urls:
        lines.append("浏览器访问：" + " | ".join(session.urls[-10:]))
    return "\n".join(lines)


def _render_work_body(session: WorkSession) -> str:
    start = (session.started_at or dt.datetime.now().astimezone()).strftime("%Y-%m-%d %H:%M:%S")
    end = (session.last_seen or dt.datetime.now().astimezone()).strftime("%Y-%m-%d %H:%M:%S")
    minutes = int(session.duration_seconds // 60)
    lines = [
        f"# {session.topic()}",
        "",
        "## 基本信息",
        "",
        f"- 进程：{session.process_name}",
        f"- 窗口标题：{session.title}",
        f"- 开始：{start}",
        f"- 结束：{end}",
        f"- 持续约：{minutes} 分钟",
        "",
    ]
    if session.urls:
        lines += ["## 浏览器访问", ""]
        lines += [f"- {u}" for u in session.urls[-20:]]
        lines.append("")
    if session.domain_hits:
        lines += ["## 访问域名统计", ""]
        for domain, count in sorted(session.domain_hits.items(), key=lambda kv: -kv[1])[:10]:
            lines.append(f"- {domain}：{count} 次")
        lines.append("")
    if session.process_snapshots:
        lines += ["## 任务管理器快照（摘录）", ""]
        lines += [f"- {s}" for s in session.process_snapshots[:6]]
        lines.append("")
    lines += ["## 备注", "", "本文件由活动监视器生成，可手动编辑补充。", ""]
    return "\n".join(lines)


def record_config_changes(ctx: AppContext, changes: list[dict[str, Any]],
                          user_note: str = "") -> Any:
    """配置每次修改都强制写入目录文件与目的文件。"""
    if not changes:
        return None
    return ctx.memory.record_config_change(changes, user_note)


# ---------- 无 GUI 的简单 CLI ----------
def run_cli(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    command = argv[0] if argv else "chat"

    if command == "chat":
        ctx = build_context()
        if not ctx.harness.is_configured():
            print("未检测到 DeepSeek API Key。请设置环境变量 DEEPSEEK_API_KEY，或在 config.json 中填写。")
            return 2
        print(f"已连接 {ctx.harness.describe()}")
        print(f"正在和 {ctx.config.assistant_name} 聊天。输入 /new 清空会话，/memory 查看目录，/quit 退出。")
        while True:
            try:
                text = input("你：").strip()
            except (EOFError, KeyboardInterrupt):
                break
            if not text:
                continue
            if text.lower() in ("/quit", "/exit"):
                break
            if text == "/new":
                ctx.engine.reset_conversation()
                print("会话已重置。")
                continue
            if text == "/memory":
                print(ctx.memory.directory_digest_text(limit=20))
                continue
            print(f"{ctx.config.assistant_name}：", end="", flush=True)
            full = ""
            for token in ctx.engine.stream_reply(text):
                full += token
                print(token, end="", flush=True)
            print()
            result = ctx.engine.finalize_turn(text, full)
            stored = result.get("entry")
            if stored:
                print(f"\n[已写入长期记忆：{stored.id} - {stored.topic}]")
            else:
                print(f"\n[本次对话未达到写入阈值（{result['judge'].get('importance', 0):.2f}）]")
        return 0

    if command == "memory":
        ctx = build_context()
        sub = argv[1] if len(argv) > 1 else "list"
        if sub == "list":
            entries = ctx.memory.sorted_entries(limit=200)
            print(f"共 {len(entries)} 条：")
            for e in entries:
                print(f"  {e.id}  w={e.effective_weight():.3f}  {e.time}  {e.type:<8} {e.topic}")
        elif sub == "show":
            if len(argv) < 3:
                print("用法：python run.py memory show <ID>")
                return 2
            e = ctx.memory.get_entry(argv[2])
            if not e:
                print("未找到该记忆。")
                return 1
            print(ctx.memory.purpose_text_for_llm(e))
        else:
            print(ctx.memory.read_directory_text())
        return 0

    if command == "doctor":
        ctx = build_context()
        print("项目目录：", APP_DIR)
        print("目录文件：", ctx.memory.directory_file)
        print("目的文件目录：", ctx.memory.purpose_dir)
        print("DeepSeek：", ctx.harness.describe())
        print("活动监视器：", "开启" if ctx.monitor.enabled else "关闭")
        print("记忆条目数：", len(ctx.memory.list_entries()))
        return 0

    print(__doc__)
    return 2


__all__ = [
    "AppContext", "build_context", "record_work_session", "record_config_changes",
    "run_cli", "sync_external_config_changes", "mark_config_recorded",
]
