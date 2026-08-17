"""活动监视器：感知“用户正在做什么 / 过去做了什么”。

数据来源：
1. 前台窗口标题 + 进程名（任务/工作上下文）；
2. 任务管理器式进程快照（CPU/内存 Top N，只保留摘要，不保留敏感内存内容）；
3. 浏览器历史（Chrome/Edge，默认关闭，需用户主动开启）。

工作会话结束后会交给“独立重要性判定程序”，足够重要的会话才会写入
目录文件与目的文件。监视器本身不直接写记忆，只通过 on_session 回调上报。
"""
from __future__ import annotations

import datetime as dt
import threading
from dataclasses import dataclass, field
from typing import Any, Callable

try:
    import psutil
except Exception:  # pragma: no cover
    psutil = None

from .browser_history import BrowserHistoryReader
from .window_probe import WindowInfo, WindowProbe


@dataclass
class WorkSession:
    key: str
    title: str
    process_name: str
    executable: str = ""
    started_at: dt.datetime | None = None
    last_seen: dt.datetime | None = None
    active_seconds: float = 0.0
    urls: list[str] = field(default_factory=list)
    domain_hits: dict[str, int] = field(default_factory=dict)
    process_snapshots: list[str] = field(default_factory=list)

    @property
    def duration_seconds(self) -> float:
        if not self.started_at:
            return 0.0
        end = self.last_seen or dt.datetime.now().astimezone()
        return max(0.0, (end - self.started_at).total_seconds())

    def topic(self) -> str:
        title = (self.title or "").strip()
        if not title:
            return f"正在使用 {self.process_name or '未知程序'}"
        return title[:60]


class ActivityMonitor(threading.Thread):
    def __init__(self, config: Any):
        super().__init__(daemon=True, name="activity-monitor")
        self.config = config
        cfg = config.get("activity_monitor", {})
        self.enabled = bool(cfg.get("enabled", True))
        self.interval = max(2, int(cfg.get("interval_seconds", 6)))
        self.browser_interval = max(10, int(cfg.get("browser_interval_seconds", 60)))
        self.session_min_seconds = int(cfg.get("session_min_seconds", 45))
        self.flush_after_seconds = int(cfg.get("flush_after_seconds", 1800))
        self.top_n = int(cfg.get("task_snapshot_top_n", 5))
        self.browser_enabled = bool(cfg.get("browser_history_enabled", False))
        self.window_enabled = bool(cfg.get("window_title_enabled", True))
        self.process_enabled = bool(cfg.get("process_snapshot_enabled", True))
        self.ignored = set(cfg.get("ignored_processes", []))
        self.probe = WindowProbe()
        self.browser_reader = BrowserHistoryReader(int(cfg.get("max_history_age_days", 7)))
        self._stop_event = threading.Event()
        self._lock = threading.RLock()
        self._current: WorkSession | None = None
        self._last_finished: WorkSession | None = None
        self._last_browser_fetch: dt.datetime | None = None
        self._last_processes: list[str] = []
        self.on_session: Callable[[WorkSession], None] | None = None

    # ---------- 线程控制 ----------
    def stop(self) -> None:
        self._stop_event.set()

    def run(self) -> None:
        if not self.enabled:
            return
        while not self._stop_event.is_set():
            try:
                self._tick()
            except Exception:
                # 监视器单次异常不应导致程序崩溃
                pass
            self._stop_event.wait(self.interval)

    def _tick(self) -> None:
        with self._lock:
            now = dt.datetime.now().astimezone()
            if self.window_enabled:
                self._update_session(self.probe.probe(), now)
            if self.process_enabled:
                self._last_processes = self._process_snapshot(now)
                if self._current is not None:
                    self._current.process_snapshots = (self._last_processes + self._current.process_snapshots)[:6]
            if self.browser_enabled and (self._last_browser_fetch is None
                                         or (now - self._last_browser_fetch).total_seconds() >= self.browser_interval):
                self._last_browser_fetch = now
                self._ingest_browser()
            # 超长会话拆段，避免一个目的文件无限膨胀
            if self._current is not None and self._current.duration_seconds >= self.flush_after_seconds:
                self._finish_current("时长分段")

    def _update_session(self, win: WindowInfo, now: dt.datetime) -> None:
        if not win.ok or not (win.title or win.process_name):
            return
        if win.process_name and any(win.process_name.lower() == i.lower() for i in self.ignored):
            return
        key = f"{win.process_name}|{win.title}"
        if self._current is not None and self._current.key == key:
            elapsed = (now - (self._current.last_seen or now)).total_seconds()
            self._current.active_seconds += max(0.0, min(elapsed, self.interval * 2))
            self._current.last_seen = now
            return
        if self._current is not None:
            self._finish_current("窗口切换")
        if self._current is None:
            self._current = WorkSession(
                key=key, title=win.title, process_name=win.process_name,
                executable=win.executable, started_at=now, last_seen=now,
            )

    def _process_snapshot(self, now: dt.datetime) -> list[str]:
        if psutil is None:
            return []
        rows: list[tuple[float, float, str]] = []
        try:
            for proc in psutil.process_iter(["name", "cpu_percent", "memory_info"]):
                try:
                    name = proc.info.get("name") or ""
                    if any(name.lower() == i.lower() for i in self.ignored):
                        continue
                    mem_mb = 0.0
                    mem = proc.info.get("memory_info")
                    if mem is not None:
                        mem_mb = getattr(mem, "rss", 0) / 1048576.0
                    rows.append((float(proc.info.get("cpu_percent") or 0), mem_mb, name))
                except Exception:
                    continue
        except Exception:
            return []
        rows.sort(key=lambda r: (r[0], r[1]), reverse=True)
        stamp = now.strftime("%H:%M:%S")
        return [f"{stamp} CPU {cpu:.0f}% 内存 {mem:.0f}MB {name}" for cpu, mem, name in rows[:self.top_n]]

    def _ingest_browser(self) -> None:
        visits = self.browser_reader.fetch_recent()
        if not visits or self._current is None:
            return
        for v in visits:
            if v.url not in self._current.urls[-40:]:
                self._current.urls.append(v.url)
            self._current.domain_hits[v.domain] = self._current.domain_hits.get(v.domain, 0) + 1

    # ---------- 会话收尾 ----------
    def _finish_current(self, reason: str) -> None:
        session = self._current
        if session is None:
            return
        session.last_seen = dt.datetime.now().astimezone()
        if session.duration_seconds >= self.session_min_seconds:
            self._last_finished = session
            callback = self.on_session
            if callback is not None:
                try:
                    callback(session)
                except Exception:
                    pass
        self._current = None

    def flush(self) -> None:
        with self._lock:
            self._finish_current("手动刷新")

    # ---------- 给聊天引擎的实时上下文 ----------
    def context_for_chat(self) -> str:
        with self._lock:
            lines: list[str] = []
            now = dt.datetime.now().astimezone()
            if self._current is not None:
                lines.append(
                    f"[实时活动] 正在使用：{self._current.process_name} - {self._current.title}，"
                    f"已持续 {int(self._current.duration_seconds // 60)} 分钟"
                )
                if self._current.urls:
                    recent = self._current.urls[-5:]
                    lines.append("最近浏览：" + " | ".join(recent))
            if self._last_finished is not None:
                lines.append(
                    f"[刚结束] {self._last_finished.process_name} - {self._last_finished.title}，"
                    f"持续约 {int(self._last_finished.duration_seconds // 60)} 分钟"
                )
            if self._last_processes:
                lines.append("[进程快照] " + "；".join(self._last_processes[:5]))
            return "\n".join(lines)


__all__ = ["ActivityMonitor", "WorkSession"]
