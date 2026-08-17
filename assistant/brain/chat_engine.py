"""聊天引擎：目录优先记忆检索 + DeepSeek 工具调用 + 本地文件工具。

每次用户发消息时：
1. 只把“目录文件摘要”放进上下文，让 DeepSeek 选择相关目的文件；
2. 打开被选中的目的文件，组装系统提示词；
3. 如果用户允许，模型可以通过函数调用直接 read/write/edit/append/
   list/delete 工作区文件（权限由 FileToolExecutor 统一限制）；
4. 所有“思考过程”以 TurnEvent 结构化事件输出，GUI 可以折叠/展开。

回答结束后，调用 finalize_turn() 由独立重要性判定程序决定是否写入记忆。
"""
from __future__ import annotations

import datetime as dt
import json
import re
import threading
from dataclasses import dataclass
from typing import Any, Iterator

from ..config import ConfigManager
from ..judge import ActivityEvent, ImportanceJudge
from ..memory import MemoryEntry, MemoryManager
from ..tools import FileToolExecutor
from .deepseek_harness import DeepSeekError, DeepSeekHarness

_RETRIEVAL_SYSTEM = (
    "你是记忆检索器。你的唯一任务是根据用户当前问题，从下面的“目录文件摘要”中"
    "选择需要打开的目的文件。目录中每一条都带有 ID。\n"
    "规则：\n"
    "1. 只能选择目录中确实存在的 ID；\n"
    "2. 不相关时返回空数组；\n"
    "3. 只输出 JSON 对象，格式：{\"ids\":[\"ID1\"]}，不要输出其他文字。"
)


@dataclass
class TurnEvent:
    """一次对话回合中的结构化事件。

    kind:
      - stage    阶段变化（正在检索、正在思考等）
      - thinking 思考过程 / 推理内容 / 工具执行结果
      - tool     工具调用记录
      - answer   最终回答（完整文本，可在 GUI 中分段显示）
    """
    kind: str
    text: str = ""


class ChatEngine:
    def __init__(self, config: ConfigManager, memory: MemoryManager,
                 judge: ImportanceJudge, harness: DeepSeekHarness,
                 monitor: Any = None):
        self.config = config
        self.memory = memory
        self.judge = judge
        self.harness = harness
        self.monitor = monitor
        self.file_tools = FileToolExecutor(config)
        self._lock = threading.RLock()
        self.history: list[dict[str, str]] = []
        self.last_turn_info: dict[str, Any] = {}

    # ---------- 配置快捷读取 ----------
    def _mem_cfg(self) -> dict[str, Any]:
        return self.config.get("memory", {})

    def _ds_cfg(self) -> dict[str, Any]:
        return self.config.get("deepseek", {})

    def _tools_cfg(self) -> dict[str, Any]:
        return self.config.get("tools", {})

    def reset_conversation(self) -> None:
        with self._lock:
            self.history.clear()

    # ---------- 记忆检索 ----------
    def select_purpose_entries(self, user_text: str, limit: int | None = None) -> list[MemoryEntry]:
        mem = self._mem_cfg()
        limit = limit or int(mem.get("max_directory_entries", 120))
        half_life = float(mem.get("decay_half_life_days", 30.0))
        directory = self.memory.directory_digest_text(limit=limit, half_life_days=half_life)
        all_entries = self.memory.sorted_entries(limit=limit, half_life_days=half_life)
        valid_ids = {e.id.lower(): e for e in all_entries}
        real_entries = [e for e in all_entries if e.id != "WELCOME-0000"]
        if not real_entries:
            return []

        messages = [
            {"role": "system", "content": _RETRIEVAL_SYSTEM},
            {"role": "user", "content": (
                f"目录文件摘要：\n{directory}\n\n"
                f"用户当前问题：{user_text}\n"
                "请选择需要打开的目的文件 ID。"
            )},
        ]
        try:
            data = self.harness.chat_json(
                messages,
                temperature=float(self._ds_cfg().get("retrieval_temperature", 0.1)),
                max_tokens=int(self._ds_cfg().get("retrieval_max_tokens", 500)),
            )
            ids = list(data.get("ids") or []) if isinstance(data, dict) else []
            selected: list[MemoryEntry] = []
            seen: set[str] = set()
            for raw_id in ids:
                entry = valid_ids.get(str(raw_id).lower())
                if entry and entry.id not in seen:
                    selected.append(entry)
                    seen.add(entry.id)
            if selected:
                return selected[: int(mem.get("max_selected_purposes", 4))]
        except Exception:
            pass
        return self._local_retrieval(user_text, all_entries,
                                     int(mem.get("max_selected_purposes", 4)))

    def _local_retrieval(self, user_text: str, entries: list[MemoryEntry],
                         max_n: int = 4) -> list[MemoryEntry]:
        tokens = _bigrams(str(user_text or ""))
        scored: list[tuple[float, MemoryEntry]] = []
        for e in entries:
            if e.id == "WELCOME-0000" or not e.purpose:
                continue
            hay = _bigrams(f"{e.topic} {e.summary} {' '.join(e.tags or [])} {' '.join(e.keywords or [])}")
            overlap = len(tokens & hay)
            base = float(e.weight or 0.5)
            scored.append((overlap * 0.7 + base * 0.3, e))
        scored.sort(key=lambda pair: pair[0], reverse=True)
        return [e for s, e in scored if s > 0.15][:max_n]

    # ---------- 上下文组装 ----------
    def _system_prompt(self, directory: str, purposes: str, activity: str,
                       tools_enabled: bool) -> str:
        now = dt.datetime.now().astimezone().strftime("%Y-%m-%d %H:%M %Z")
        parts = [
            self.config.persona_prompt,
            f"当前时间：{now}",
            "记忆规则：你的长期记忆分为“目录文件”和“目的文件”。下面是目录文件摘要；"
            "只有在用户明确聊到相关主题时，才使用已打开的目的文件内容。"
            "如果目录里没有相关内容，就诚实地说“我暂时没有相关记忆”，不要编造。",
            directory,
            purposes if purposes else "（本次没有打开任何目的文件）",
            activity if activity else "（暂无实时活动上下文）",
        ]
        if tools_enabled:
            parts.append(
                "本地文件工具：你可以调用 read_file / write_file / edit_file / "
                "append_file / list_directory / delete_file 直接操作用户工作区内的文件。"
                "调用写操作前，先用一两句话说明你要做什么；如果工具返回权限拒绝，"
                "不要尝试绕过，而要向用户解释当前权限限制。"
            )
        else:
            parts.append("本地文件工具已被用户关闭，不要调用任何文件工具。")
        parts.append(
            "工作规则：结合用户正在做/过去做过的事，主动帮助用户完成工作；"
            "回答保持清晰、结构化；需要更多信息时温柔地追问。"
        )
        return "\n\n".join(parts)

    def _activity_context(self) -> str:
        if self.monitor is None:
            return ""
        try:
            return self.monitor.context_for_chat() or ""
        except Exception:
            return ""

    # ---------- 核心：结构化回合流 ----------
    def stream_turn(self, user_text: str) -> Iterator[TurnEvent]:
        tool_log: list[str] = []
        with self._lock:
            yield TurnEvent("stage", "正在阅读目录文件，寻找相关记忆…")
            directory, selected = self.build_context(user_text)
            if selected:
                yield TurnEvent("thinking", "从目录中选中目的文件：\n" + "\n".join(
                    f"  - {e.id}｜{e.topic}｜{e.purpose}" for e in selected))
            else:
                yield TurnEvent("thinking", "目录中暂时没有与本次问题直接相关的目的文件。")
            max_chars = int(self._mem_cfg().get("max_purpose_chars", 14000))
            purposes = "\n\n".join(
                self.memory.purpose_text_for_llm(e, max_chars) for e in selected
            )
            activity = self._activity_context()
            tools_enabled = bool(self._tools_cfg().get("enabled", True))
            system = self._system_prompt(directory, purposes, activity, tools_enabled)
            messages: list[dict[str, Any]] = [{"role": "system", "content": system}]
            for msg in self.history[-16:]:
                messages.append({"role": msg["role"], "content": msg["content"]})
            messages.append({"role": "user", "content": user_text})
            self.last_turn_info = {
                "user_text": user_text,
                "selected_ids": [e.id for e in selected],
                "tool_log": tool_log,
            }

        try:
            yield from self._tool_loop(messages, tool_log)
        except DeepSeekError as exc:
            self.last_turn_info["stream_error"] = str(exc)
            yield TurnEvent("thinking", f"DeepSeek 调用失败：{exc}")
            yield TurnEvent("answer", f"\n\n[助手暂时无法连接 DeepSeek] {exc}")
        except Exception as exc:
            self.last_turn_info["stream_error"] = str(exc)
            yield TurnEvent("thinking", f"助手内部错误：{exc}")
            yield TurnEvent("answer", f"\n\n[助手出错] {exc}")

    def _tool_loop(self, messages: list[dict[str, Any]], tool_log: list[str]) -> Iterator[TurnEvent]:
        definitions = self.file_tools.tool_definitions()
        max_rounds = int(self._tools_cfg().get("max_tool_rounds", 6))
        answer = ""
        for round_no in range(max_rounds):
            yield TurnEvent("stage", f"正在思考（第 {round_no + 1} 轮）…")
            msg = self.harness.chat_full(
                messages,
                temperature=float(self._ds_cfg().get("temperature", 0.7)),
                max_tokens=int(self._ds_cfg().get("max_tokens", 2048)),
                tools=definitions or None,
            )
            reasoning = (msg.get("reasoning_content") or "").strip()
            if reasoning:
                yield TurnEvent("thinking", "模型推理：\n" + reasoning)
            tool_calls = msg.get("tool_calls") or []
            if tool_calls:
                api_tool_calls = []
                for tc in tool_calls:
                    fn = tc.get("function") or {}
                    api_tool_calls.append({
                        "id": tc.get("id") or "",
                        "type": "function",
                        "function": {
                            "name": fn.get("name") or "",
                            "arguments": json.dumps(fn.get("arguments") or {}, ensure_ascii=False),
                        },
                    })
                messages.append({
                    "role": "assistant",
                    "content": msg.get("content") or "",
                    "tool_calls": api_tool_calls,
                })
                for tc in tool_calls:
                    fn = tc.get("function") or {}
                    name = fn.get("name") or "unknown"
                    args = fn.get("arguments") or {}
                    try:
                        arg_text = json.dumps(args, ensure_ascii=False, indent=2)
                    except Exception:
                        arg_text = str(args)
                    yield TurnEvent("tool", f"调用工具 {name}")
                    yield TurnEvent("thinking", f"{name} 参数：\n{arg_text}")
                    result = self.file_tools.execute(name, args)
                    result_text = json.dumps(result, ensure_ascii=False)
                    tool_log.append(f"{name}({json.dumps(args, ensure_ascii=False)}) -> {result_text[:400]}")
                    self.last_turn_info["tool_log"] = tool_log
                    yield TurnEvent("thinking", f"{name} 返回：\n{_clip(result_text, 900)}")
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc.get("id") or "",
                        "content": result_text,
                    })
                continue

            answer = str(msg.get("content") or "").strip()
            if answer:
                self.last_turn_info["answer"] = answer
                self.last_turn_info["tool_log"] = tool_log
                yield TurnEvent("answer", answer)
                return
            yield TurnEvent("thinking", "模型本轮没有输出内容，继续请求。")
        self.last_turn_info["answer"] = answer or "抱歉，这轮思考有点久，请换个说法再试一次。"
        yield TurnEvent("answer", self.last_turn_info["answer"])

    def build_context(self, user_text: str) -> tuple[str, list[MemoryEntry]]:
        selected = self.select_purpose_entries(user_text)
        mem = self._mem_cfg()
        directory = self.memory.directory_digest_text(
            limit=int(mem.get("max_directory_entries", 120)),
            half_life_days=float(mem.get("decay_half_life_days", 30.0)),
        )
        return directory, selected

    # ---------- 兼容旧的纯文本流式接口 ----------
    def stream_reply(self, user_text: str) -> Iterator[str]:
        for event in self.stream_turn(user_text):
            if event.kind == "answer":
                yield event.text

    # ---------- 对话结束后的记忆写入 ----------
    def finalize_turn(self, user_text: str, assistant_text: str) -> dict[str, Any]:
        if self.last_turn_info.get("stream_error"):
            self.last_turn_info.update({
                "judge": {"importance": 0.0, "threshold": 0.45, "should_store": False,
                          "reasons": ["流式回复失败，不写入长期记忆"]},
                "stored_entry_id": None,
            })
            return {"judge": self.last_turn_info["judge"], "entry": None}
        tool_log = self.last_turn_info.get("tool_log") or []
        tags = self._tags_from_text(user_text)
        explicit = None
        write_names = ("write_file(", "append_file(", "edit_file(", "delete_file(")
        if any(name in line for name in write_names for line in tool_log):
            tags = list(dict.fromkeys(tags + ["文件操作", "本地修改"]))
            explicit = 1.0  # 文件写操作属于必须留痕的重要工作
        event = ActivityEvent(
            topic=self._topic_from_turn(user_text, assistant_text),
            occurred_at=dt.datetime.now().astimezone(),
            source="chat",
            detail=f"用户：{user_text}\n助手：{assistant_text[:600]}\n工具：{'; '.join(tool_log)[:500]}",
            message_count=2,
            explicit_importance=explicit,
            tags=tags,
            keywords=self._tags_from_text(user_text),
        )
        self.judge.set_directory_entries(self.memory.list_entries())
        result = self.judge.judge(event)
        entry: MemoryEntry | None = None
        if result.should_store:
            messages: list[dict[str, str]] = [
                {"role": "user", "content": user_text},
                {"role": "assistant", "content": assistant_text},
            ]
            for line in (self.last_turn_info.get("tool_log") or []):
                messages.append({"role": "tool", "content": line})
            entry = self.memory.record_chat(
                event.topic,
                messages,
                importance=result.importance,
                weight=result.weight,
                tags=event.tags,
                keywords=event.keywords,
                reasons=result.reasons,
                source="chat",
                user_name="用户",
                assistant_name=self.config.assistant_name,
            )
        # 把本轮对话写入会话历史，供后续追问使用；避免重复追加
        if not self.last_turn_info.get("history_committed"):
            self.history.append({"role": "user", "content": user_text})
            self.history.append({"role": "assistant", "content": assistant_text})
            # 只保留最近若干轮，防止上下文无限膨胀
            if len(self.history) > 40:
                self.history = self.history[-40:]
            self.last_turn_info["history_committed"] = True
        self.last_turn_info.update({
            "judge": result.to_dict(),
            "stored_entry_id": entry.id if entry else None,
        })
        return {"judge": result.to_dict(), "entry": entry}

    # ---------- 工具方法 ----------
    @staticmethod
    def _topic_from_turn(user: str, assistant: str) -> str:
        user = re.sub(r"\s+", " ", str(user or "")).strip()
        if not user:
            return "未命名对话"
        return user[:48] + ("…" if len(user) > 48 else "")

    @staticmethod
    def _tags_from_text(text: str) -> list[str]:
        words = ["项目", "工作", "学习", "代码", "bug", "文档", "设计", "会议",
                 "写作", "数据", "配置", "重要", "待办", "生活", "闲聊"]
        found = [w for w in words if w in str(text)]
        return found or ["对话"]


def _clip(text: str, limit: int) -> str:
    text = str(text or "")
    return text[:limit] + ("…" if len(text) > limit else "")


def _bigrams(text: str) -> set[str]:
    text = re.sub(r"\W+", "", str(text or ""), flags=re.UNICODE).lower()
    return {text[i:i + 2] for i in range(max(0, len(text) - 1))}
