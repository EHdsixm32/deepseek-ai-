"""独立的重要性判定程序（可接入 AI，也可完全离线运行）。

职责：
1. 判断某次对话 / 工作 / 浏览记录是否“值得写入目录文件和目的文件”；
2. 为目录文件中的每条任务计算权重（importance * 来源可信度 * 时间衰减 + 频率增益）；
3. 支持命令行单独运行：python -m assistant.judge.importance。

规则模型（默认离线）：
    score = 0.34*explicit + 0.28*semantic + 0.22*duration + 0.16*frequency
    weight = clamp(importance * recency_decay * source_bias + frequency_boost)

语义分默认由关键词、任务时长、来源综合得到；设置 judge.use_ai=true 后，
会额外调用 DeepSeek 给 0~1 的语义重要性分。
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import re
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# 高重要度信号词（中英文混合；可自行扩展）
_STRONG_KEYWORDS = [
    "重要", "紧急", "记住", "deadline", "截止", "必须", "关键", "合同", "面试",
    "决定", "结论", "todo", "待办", "bug", "故障", "风险", "密码", "账号",
    "发布", "上线", "考试", "提交", "老板", "客户", "final", "urgent",
]
_WEAK_KEYWORDS = [
    "学习", "项目", "计划", "需求", "设计", "开发", "测试", "文档", "会议",
    "research", "study", "笔记", "整理", "总结",
]
_SKIP_KEYWORDS = ["闲聊", "天气", "摸鱼", "表情包"]
_MEMORY_COMMANDS = ["记住", "别忘了", "记一下", "记下来", "不要忘", "提醒我", "remember", "remind me"]

_SOURCE_BASE = {
    "user_explicit": 0.95,
    "config": 0.9,
    "chat": 0.72,
    "work": 0.68,
    "browser": 0.5,
    "task_manager": 0.38,
    "note": 0.6,
}


@dataclass
class ActivityEvent:
    """待判定的活动事件。"""
    topic: str
    occurred_at: dt.datetime | None = None
    source: str = "chat"          # chat/work/browser/task_manager/config/note/user_explicit
    detail: str = ""
    duration_seconds: float = 0.0
    message_count: int = 1
    explicit_importance: float | None = None   # 用户显式指定 0~1
    tags: list[str] = field(default_factory=list)
    keywords: list[str] = field(default_factory=list)
    url: str = ""
    process: str = ""
    window_title: str = ""

    def age_days(self, now: dt.datetime | None = None) -> float:
        now = now or dt.datetime.now().astimezone()
        when = self.occurred_at or now
        return max(0.0, (now - when).total_seconds() / 86400.0)


@dataclass
class JudgeResult:
    importance: float = 0.0
    weight: float = 0.0
    score: float = 0.0
    should_store: bool = False
    threshold: float = 0.45
    reasons: list[str] = field(default_factory=list)
    ai_importance: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "importance": round(self.importance, 4),
            "weight": round(self.weight, 4),
            "score": round(self.score, 4),
            "should_store": self.should_store,
            "threshold": self.threshold,
            "reasons": list(self.reasons),
            "ai_importance": self.ai_importance,
        }


def clamp(value: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, float(value)))


def recency_decay(age_days: float, half_life_days: float = 14.0) -> float:
    """时间越近越重要，符合“相隔时间影响权重”的要求。"""
    if age_days <= 0:
        return 1.0
    half_life = max(0.5, float(half_life_days))
    return max(0.05, math.exp(-math.log(2) * age_days / half_life))


class ImportanceJudge:
    """独立重要性判定器。

    - 不依赖任何数据库，纯 Python 可单独运行；
    - use_ai=True 时调用 DeepSeek（可接入 AI）；
    - directory_entries 传入现有目录条目，用于同主题频率增益。
    """

    def __init__(self, config: Any = None, directory_entries: list[Any] | None = None,
                 deepseek: Any = None):
        self.config = config
        if config is not None:
            jc = config.get("judge", {})
            self.threshold = float(jc.get("threshold", 0.45))
            self.recency_half_life = float(jc.get("recency_half_life_days", 14.0))
            self.use_ai = bool(jc.get("use_ai", False))
        else:
            self.threshold = 0.45
            self.recency_half_life = 14.0
            self.use_ai = False
        self.directory_entries = list(directory_entries or [])
        self.deepseek = deepseek
        self._history: list[tuple[dt.datetime, str, float]] = []
        self._lock = threading.RLock()

    def set_directory_entries(self, entries: list[Any]) -> None:
        with self._lock:
            self.directory_entries = list(entries or [])

    def _frequency_boost(self, event: ActivityEvent) -> float:
        topic = _normalize(event.topic)
        if not topic:
            return 0.0
        hits = 0
        for e in self.directory_entries:
            old_topic = _normalize(getattr(e, "topic", ""))
            if topic and old_topic and (topic in old_topic or old_topic in topic):
                hits += 1
        # 同类任务反复出现说明是长期主线：最多加 0.2
        return min(0.2, 0.04 * hits)

    def _semantic_score(self, event: ActivityEvent) -> float:
        text = " ".join([
            event.topic, event.detail, event.window_title,
            " ".join(event.tags), " ".join(event.keywords),
        ]).lower()
        strong = sum(1 for w in _STRONG_KEYWORDS if w in text)
        weak = sum(1 for w in _WEAK_KEYWORDS if w in text)
        skip = sum(1 for w in _SKIP_KEYWORDS if w in text)
        base = 0.42 + 0.10 * strong + 0.04 * weak
        if skip and strong == 0:
            base -= 0.25
        return clamp(base)

    def _duration_score(self, event: ActivityEvent) -> float:
        # 工作时间越长越重要，60 分钟封顶
        return clamp(event.duration_seconds / 3600.0)

    def _explicit_score(self, event: ActivityEvent) -> float:
        if event.explicit_importance is not None:
            return clamp(event.explicit_importance)
        text = " ".join([event.topic, event.detail, event.window_title]).lower()
        if any(w in text for w in _MEMORY_COMMANDS):
            return 0.92  # “记住 / 别忘了”视作用户显式记忆要求
        # 没有显式分数时，用来源基准作为“用户意图”近似
        return _SOURCE_BASE.get(event.source, 0.5)

    def _rule_score(self, event: ActivityEvent) -> JudgeResult:
        explicit = self._explicit_score(event)
        semantic = self._semantic_score(event)
        duration = self._duration_score(event)
        freq = self._frequency_boost(event)
        age = event.age_days()
        recency = recency_decay(age, self.recency_half_life)

        score = 0.34 * explicit + 0.28 * semantic + 0.22 * duration + 0.16 * freq
        importance = clamp(score)
        # 权重：重要性为主，叠加来源偏差、时间衰减和频率增益
        source_bias = _SOURCE_BASE.get(event.source, 0.5)
        weight = clamp(0.55 * importance + 0.15 * source_bias + 0.20 * recency + 0.10 * freq)

        reasons = [
            f"来源={event.source}(基准{source_bias:.2f})",
            f"语义分={semantic:.2f}",
            f"时长/规模分={duration:.2f}",
            f"主题频率增益=+{freq:.2f}",
            f"时间衰减={recency:.2f}（距今{age:.1f}天）",
        ]
        if event.explicit_importance is not None:
            reasons.insert(0, f"用户显式重要度={explicit:.2f}")
        elif explicit > _SOURCE_BASE.get(event.source, 0.5):
            reasons.insert(0, "检测到显式记忆指令（记住/别忘了）")
        result = JudgeResult(
            importance=importance, weight=weight, score=score,
            threshold=self.threshold, reasons=reasons,
        )
        result.should_store = importance >= self.threshold
        return result

    def _ai_score(self, event: ActivityEvent) -> float | None:
        if not self.use_ai or self.deepseek is None:
            return None
        if not getattr(self.deepseek, "is_configured", lambda: False)():
            return None
        prompt = (
            "你是活动重要性评估器。根据主题、来源、时长、标签和关键词，"
            "给出0到1的重要性分数（1=极其重要，必须写入长期记忆；0=完全无关）。"
            "只输出JSON对象：{\"importance\":0.0,\"reason\":\"...\"}\n"
            f"主题：{event.topic}\n来源：{event.source}\n"
            f"时长秒：{event.duration_seconds:.0f}\n标签：{event.tags}\n"
            f"关键词：{event.keywords}\n细节：{event.detail[:400]}\n"
            f"窗口标题：{event.window_title[:200]}"
        )
        try:
            raw = self.deepseek.chat_json(
                [{"role": "user", "content": prompt}],
                temperature=0.1, max_tokens=200,
            )
            data = json.loads(raw) if isinstance(raw, str) else raw
            return clamp(float(data.get("importance", 0.5)))
        except Exception:
            return None

    def judge(self, event: ActivityEvent, use_ai: bool | None = None) -> JudgeResult:
        with self._lock:
            result = self._rule_score(event)
            if use_ai is None:
                use_ai = self.use_ai
            if use_ai:
                ai = self._ai_score(event)
                if ai is not None:
                    result.ai_importance = round(ai, 4)
                    result.importance = clamp(0.45 * result.importance + 0.55 * ai)
                    result.weight = clamp(0.55 * result.importance + 0.15 * result.weight)
                    result.reasons.append(f"AI语义重要度={ai:.2f}")
            result.should_store = result.importance >= self.threshold or event.source == "config"
            result.threshold = self.threshold
            return result

    # ---------- 目录文件权重重算 ----------
    def recalc_entry_weight(self, entry: Any, now: dt.datetime | None = None) -> float:
        """重算目录文件中单条记忆的权重（重要性 * 时间衰减 + 来源 + 频率）。"""
        try:
            occurred = entry.occurred_at or (now or dt.datetime.now().astimezone())
            age = max(0.0, ((now or dt.datetime.now().astimezone()) - occurred).total_seconds() / 86400.0)
        except Exception:
            age = 999.0
        recency = recency_decay(age, self.recency_half_life)
        source_bias = _SOURCE_BASE.get(getattr(entry, "type", "note"), 0.6)
        freq = self._frequency_boost(ActivityEvent(topic=getattr(entry, "topic", "")))
        importance = float(getattr(entry, "importance", 0.5) or 0.5)
        return clamp(0.55 * importance + 0.15 * source_bias + 0.20 * recency + 0.10 * freq)

    def recalc_directory(self, entries: list[Any]) -> list[tuple[Any, float]]:
        result = []
        for e in entries:
            result.append((e, self.recalc_entry_weight(e)))
        return result


def _normalize(text: str) -> str:
    return re.sub(r"\s+", "", str(text or "")).lower()


def _event_from_args(args: argparse.Namespace) -> ActivityEvent:
    occurred = None
    if getattr(args, "time", None):
        try:
            occurred = dt.datetime.fromisoformat(args.time)
        except Exception:
            occurred = dt.datetime.now().astimezone()
    return ActivityEvent(
        topic=args.topic,
        occurred_at=occurred,
        source=args.source,
        detail=getattr(args, "detail", "") or "",
        duration_seconds=float(getattr(args, "duration", 0) or 0),
        message_count=int(getattr(args, "messages", 1) or 1),
        explicit_importance=float(args.explicit) if getattr(args, "explicit", None) not in (None, "") else None,
        tags=[t for t in (getattr(args, "tags", "") or "").split(",") if t.strip()],
        keywords=[k for k in (getattr(args, "keywords", "") or "").split(",") if k.strip()],
        window_title=getattr(args, "window", "") or "",
        process=getattr(args, "process", "") or "",
    )


def _build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="AI智能助手 - 独立重要性判定程序（可离线，可接入 DeepSeek）",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    sub = p.add_subparsers(dest="command")

    q = sub.add_parser("judge", help="判定一个活动/任务是否值得写入记忆")
    q.add_argument("topic")
    q.add_argument("--source", default="chat", choices=list(_SOURCE_BASE.keys()))
    q.add_argument("--detail", default="")
    q.add_argument("--duration", type=float, default=0.0, help="持续时间（秒）")
    q.add_argument("--messages", type=int, default=1, help="聊天消息条数")
    q.add_argument("--explicit", type=float, default=None, help="用户显式重要度 0~1")
    q.add_argument("--tags", default="")
    q.add_argument("--keywords", default="")
    q.add_argument("--window", default="")
    q.add_argument("--process", default="")
    q.add_argument("--time", default="")
    q.add_argument("--threshold", type=float, default=None)
    q.add_argument("--use-ai", action="store_true", help="尝试调用 DeepSeek 语义评分")

    r = sub.add_parser("recalc", help="重算目录文件中每条记忆的权重")
    r.add_argument("--commit", action="store_true", help="把新权重写回目录文件")
    r.add_argument("--memory-root", default=None)
    return p


def _main(argv: list[str] | None = None) -> int:
    args = _build_arg_parser().parse_args(argv)
    if args.command == "judge":
        from ..config import ConfigManager
        from ..brain.deepseek_harness import DeepSeekHarness

        cfg = ConfigManager()
        judge = ImportanceJudge(cfg)
        if args.threshold is not None:
            judge.threshold = clamp(args.threshold)
            args.threshold = judge.threshold
        if args.use_ai:
            judge.use_ai = True
            judge.deepseek = DeepSeekHarness(cfg)
        result = judge.judge(_event_from_args(args))
        print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
        return 0 if result.should_store else 1

    if args.command == "recalc":
        from ..config import ConfigManager
        from ..memory import MemoryManager

        cfg = ConfigManager()
        if args.memory_root:
            memory = MemoryManager(args.memory_root, cfg)
        else:
            memory = MemoryManager(config=cfg)
        judge = ImportanceJudge(cfg)
        entries = [e for e in memory.list_entries() if e.parsed_ok and e.id != "WELCOME-0000"]
        print(f"共 {len(entries)} 条记忆：")
        for entry, new_weight in judge.recalc_directory(entries):
            print(f"  {entry.id}  {entry.topic[:30]:<30}  {entry.weight:.3f} -> {new_weight:.3f}")
            if args.commit:
                memory.update_entry_field(entry.id, "weight", round(new_weight, 4))
        if args.commit:
            print("已写回目录文件。")
        return 0

    _build_arg_parser().print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
