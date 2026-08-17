"""配置管理：所有可调参数集中在这里。

设计目标：
1. 默认参数保证程序在任何机器上都不会崩溃；
2. 用户只能通过 ConfigManager 修改参数，修改前做范围校验；
3. 参数修改会被记录进“目录文件 / 目的文件”记忆系统（由调用方完成）。
"""
from __future__ import annotations

import copy
import json
import os
import threading
from pathlib import Path
from typing import Any

APP_DIR = Path(__file__).resolve().parent.parent
MEMORY_ROOT = APP_DIR / "memory"
DIRECTORY_FILE = MEMORY_ROOT / "目录.md"
PURPOSE_DIR = MEMORY_ROOT / "目的"
DATA_DIR = APP_DIR / "data"
CONFIG_FILE = APP_DIR / "config.json"
PET_IMAGE = APP_DIR / "ds娘.png"

DEFAULT_CONFIG: dict[str, Any] = {
    "assistant_name": "DS娘",
    # 初始语气：软糯、可爱、温柔、可靠；用户可以自行替换整段提示词
    "persona": (
        "你是{name}，用户的AI智能助手。你的语气软糯、可爱、温柔、可靠："
        "用亲切自然的中文交流，像一只认真又有点粘人的小鲸鱼，"
        "偶尔用“呀、哦、呢”等语气词，但不油腻、不幼稚、不撒谎。"
        "回答要专业、准确、有结构；不确定时温柔地说明。"
    ),
    "language": "zh",
    "deepseek": {
        "api_key": "",
        "api_key_env": "DEEPSEEK_API_KEY",
        "base_url": "https://api.deepseek.com/v1",
        "model": "deepseek-chat",
        "timeout_seconds": 90,
        "temperature": 0.7,
        "max_tokens": 2048,
        # 记忆检索步骤用更低的温度，保证只按目录文件选择目的文件
        "retrieval_temperature": 0.1,
        "retrieval_max_tokens": 500,
    },
    "tools": {
        # 本地文件工具总开关；关闭后模型不能调用任何文件工具
        "enabled": True,
        # 文件工具的活动根目录。相对路径基于项目目录解析；"." 表示仅限本程序目录
        "workspace_root": ".",
        "allow_read": True,
        "allow_write": True,
        "allow_edit": True,
        "allow_delete": False,
        "allow_list": True,
        # 是否允许访问 workspace_root 之外的绝对路径（默认禁止，最安全）
        "allow_outside_workspace": False,
        # 禁止工具读取/修改 API Key、密码等敏感文件（config.json、.env 等）
        "deny_sensitive_files": True,
        # 写/改/删操作是否需要用户在弹窗中逐次批准
        "auto_approve": False,
        "max_file_bytes": 500000,
        "max_tool_rounds": 6,
    },
    "memory": {
        # 每次聊天时喂给大模型的目录条数上限（按有效权重排序）
        "max_directory_entries": 120,
        # 被选中目的文件最多读入的字符数
        "max_purpose_chars": 14000,
        # 一次检索最多打开的目的文件数
        "max_selected_purposes": 4,
        # 目录/目的文件全部使用 UTF-8 Markdown，可直接用编辑器打开
        "encoding": "utf-8",
        # 权重随时间衰减的半衰期（天）
        "decay_half_life_days": 30.0,
        # 低于该分数的事件不写入目录文件
        "default_min_importance": 0.45,
        # 配置修改永远写入记忆
        "always_store_config": True,
    },
    "judge": {
        "threshold": 0.45,
        "recency_half_life_days": 14.0,
        "use_ai": False,
    },
    "activity_monitor": {
        "enabled": True,
        "window_title_enabled": True,
        "process_snapshot_enabled": True,
        "browser_history_enabled": False,
        "interval_seconds": 6,
        "browser_interval_seconds": 60,
        "session_min_seconds": 45,
        "flush_after_seconds": 1800,
        "max_history_age_days": 7,
        "task_snapshot_top_n": 5,
        "ignored_processes": ["Idle", "System", "svchost.exe", "explorer.exe"],
    },
    "pet": {
        "size": 170,
        "opacity": 0.98,
        "click_interval_ms": 500,
        "click_count_to_open": 2,
        "always_on_top": True,
        "animation": True,
    },
    "ui": {
        "theme": "ds",
        "window_width": 920,
        "window_height": 660,
        "show_memory_panel": False,
    },
}

# 参数范围校验表：dot.path -> (类型, 最小值, 最大值, 允许值)
_RANGES: dict[str, tuple[type, Any, Any, Any]] = {
    "deepseek.temperature": (float, 0.0, 2.0, None),
    "deepseek.max_tokens": (int, 64, 32768, None),
    "deepseek.retrieval_temperature": (float, 0.0, 1.0, None),
    "deepseek.retrieval_max_tokens": (int, 16, 4096, None),
    "deepseek.timeout_seconds": (int, 5, 600, None),
    "tools.max_file_bytes": (int, 1024, 50000000, None),
    "tools.max_tool_rounds": (int, 1, 20, None),
    "memory.max_directory_entries": (int, 1, 2000, None),
    "memory.max_purpose_chars": (int, 200, 200000, None),
    "memory.max_selected_purposes": (int, 1, 20, None),
    "memory.decay_half_life_days": (float, 0.5, 3650.0, None),
    "memory.default_min_importance": (float, 0.0, 1.0, None),
    "judge.threshold": (float, 0.0, 1.0, None),
    "judge.recency_half_life_days": (float, 0.5, 3650.0, None),
    "activity_monitor.interval_seconds": (int, 2, 3600, None),
    "activity_monitor.browser_interval_seconds": (int, 10, 86400, None),
    "activity_monitor.session_min_seconds": (int, 5, 86400, None),
    "activity_monitor.flush_after_seconds": (int, 60, 86400, None),
    "activity_monitor.max_history_age_days": (int, 1, 365, None),
    "activity_monitor.task_snapshot_top_n": (int, 1, 50, None),
    "pet.size": (int, 48, 600, None),
    "pet.opacity": (float, 0.3, 1.0, None),
    "pet.click_interval_ms": (int, 200, 3000, None),
    "pet.click_count_to_open": (int, 1, 5, None),
    "ui.window_width": (int, 480, 4000, None),
    "ui.window_height": (int, 360, 3000, None),
}

_BOOL_KEYS = {
    "judge.use_ai",
    "tools.enabled",
    "tools.allow_read",
    "tools.allow_write",
    "tools.allow_edit",
    "tools.allow_delete",
    "tools.allow_list",
    "tools.allow_outside_workspace",
    "tools.deny_sensitive_files",
    "tools.auto_approve",
    "activity_monitor.enabled",
    "activity_monitor.window_title_enabled",
    "activity_monitor.process_snapshot_enabled",
    "activity_monitor.browser_history_enabled",
    "memory.always_store_config",
    "pet.always_on_top",
    "pet.animation",
    "ui.show_memory_panel",
}


class ConfigError(ValueError):
    """配置不合法时抛出，调用方可以安全地展示给用户。"""


class ConfigManager:
    """线程安全的配置读写器。只允许修改白名单/范围内的参数。"""

    def __init__(self, path: Path | str = CONFIG_FILE):
        self.path = Path(path)
        self._lock = threading.RLock()
        self._data = copy.deepcopy(DEFAULT_CONFIG)
        self.load()

    # ---------- 基础读写 ----------
    def load(self) -> None:
        with self._lock:
            if self.path.exists():
                try:
                    raw = json.loads(self.path.read_text(encoding="utf-8"))
                    merged = copy.deepcopy(DEFAULT_CONFIG)
                    _deep_merge(merged, raw if isinstance(raw, dict) else {})
                    self._data = merged
                except Exception:
                    # 配置文件损坏时退回默认值，不让程序崩溃
                    self._data = copy.deepcopy(DEFAULT_CONFIG)
            else:
                self._data = copy.deepcopy(DEFAULT_CONFIG)
                self.save()

    def save(self) -> None:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(self.path.suffix + ".tmp")
            tmp.write_text(
                json.dumps(self._data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            tmp.replace(self.path)

    def as_dict(self) -> dict[str, Any]:
        with self._lock:
            return copy.deepcopy(self._data)

    def get(self, dotted: str, default: Any = None) -> Any:
        with self._lock:
            node: Any = self._data
            for part in dotted.split("."):
                if isinstance(node, dict) and part in node:
                    node = node[part]
                else:
                    return default
            return copy.deepcopy(node)

    def set(self, dotted: str, value: Any, validate: bool = True) -> dict[str, Any]:
        """设置参数。返回实际生效后的完整配置快照。

        校验失败抛 ConfigError，程序不会进入崩溃状态。
        """
        with self._lock:
            if validate:
                value = self.validate(dotted, value)
            parts = dotted.split(".")
            node = self._data
            for part in parts[:-1]:
                if not isinstance(node.get(part), dict):
                    node[part] = {}
                node = node[part]
            old = node.get(parts[-1])
            node[parts[-1]] = value
            self.save()
            return {"path": dotted, "old": old, "new": copy.deepcopy(value)}

    @classmethod
    def validate(cls, dotted: str, value: Any) -> Any:
        if dotted in _BOOL_KEYS:
            if not isinstance(value, bool):
                raise ConfigError(f"参数 {dotted} 需要布尔值")
            return value
        rng = _RANGES.get(dotted)
        if rng is None:
            # 文本类参数：只做类型与长度校验，避免程序被超大字符串拖垮
            if not isinstance(value, str):
                raise ConfigError(f"参数 {dotted} 需要字符串")
            if len(value) > 8000:
                raise ConfigError(f"参数 {dotted} 过长（最多 8000 字符）")
            return value
        typ, lo, hi, allowed = rng
        try:
            if typ is float:
                value = float(value)
            elif typ is int:
                value = int(value)
            if lo is not None and value < lo:
                raise ConfigError(f"参数 {dotted} 不能小于 {lo}")
            if hi is not None and value > hi:
                raise ConfigError(f"参数 {dotted} 不能大于 {hi}")
            if allowed is not None and value not in allowed:
                raise ConfigError(f"参数 {dotted} 只允许 {allowed}")
            return value
        except ConfigError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise ConfigError(f"参数 {dotted} 的类型不正确") from exc

    def update_many(self, changes: dict[str, Any]) -> list[dict[str, Any]]:
        """批量修改，先全部校验再提交。返回变化列表。"""
        with self._lock:
            validated = {k: self.validate(k, v) for k, v in changes.items()}
            result = []
            for dotted, value in validated.items():
                result.append(self.set(dotted, value, validate=False))
            return result

    @property
    def assistant_name(self) -> str:
        return str(self.get("assistant_name") or "DS娘")

    @property
    def persona_prompt(self) -> str:
        base = str(self.get("persona") or "")
        return base.replace("{name}", self.assistant_name)


def _deep_merge(base: dict, override: dict) -> None:
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _deep_merge(base[key], value)
        else:
            base[key] = value


__all__ = [
    "APP_DIR", "MEMORY_ROOT", "DIRECTORY_FILE", "PURPOSE_DIR", "DATA_DIR",
    "CONFIG_FILE", "PET_IMAGE", "DEFAULT_CONFIG", "ConfigManager", "ConfigError",
]
