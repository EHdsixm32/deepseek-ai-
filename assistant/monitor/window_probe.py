"""跨平台活动感知探针：前台窗口标题 + 进程名。

Windows 使用 ctypes（不需要 pywin32）；Linux 尝试 xdotool；macOS 尝试 osascript。
"""
from __future__ import annotations

import ctypes
import shutil
import subprocess
from ctypes import wintypes
from dataclasses import dataclass

try:
    import psutil
except Exception:  # pragma: no cover - psutil 是可选运行依赖
    psutil = None


@dataclass
class WindowInfo:
    title: str = ""
    process_name: str = ""
    process_id: int | None = None
    executable: str = ""
    ok: bool = False


class WindowProbe:
    def __init__(self) -> None:
        self.backend = self._choose_backend()

    @staticmethod
    def _choose_backend() -> str:
        if shutil.which("xdotool"):
            return "xdotool"
        try:
            ctypes.windll.user32.GetForegroundWindow
            return "win32"
        except Exception:
            return "none"

    def probe(self) -> WindowInfo:
        if self.backend == "win32":
            return self._probe_win32()
        if self.backend == "xdotool":
            return self._probe_xdotool()
        return WindowInfo(ok=False)

    @staticmethod
    def _probe_win32() -> WindowInfo:
        info = WindowInfo()
        try:
            user32 = ctypes.windll.user32
            kernel32 = ctypes.windll.kernel32
            hwnd = user32.GetForegroundWindow()
            if not hwnd:
                return info
            length = user32.GetWindowTextLengthW(hwnd)
            if length <= 0:
                return info
            buf = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buf, length + 1)
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            info.title = buf.value
            info.process_id = int(pid.value) or None
            if psutil is not None and info.process_id:
                try:
                    proc = psutil.Process(info.process_id)
                    info.process_name = proc.name() or ""
                    info.executable = proc.exe() or ""
                except Exception:
                    pass
            info.ok = bool(info.title)
        except Exception:
            pass
        return info

    @staticmethod
    def _probe_xdotool() -> WindowInfo:
        info = WindowInfo()
        try:
            wid = subprocess.check_output(
                ["xdotool", "getactivewindow"], text=True, timeout=1
            ).strip()
            if not wid:
                return info
            info.title = subprocess.check_output(
                ["xdotool", "getwindowname", wid], text=True, timeout=1
            ).strip()
            pid = subprocess.check_output(
                ["xdotool", "getwindowpid", wid], text=True, timeout=1
            ).strip()
            info.process_id = int(pid) if pid.isdigit() else None
            if psutil is not None and info.process_id:
                try:
                    proc = psutil.Process(info.process_id)
                    info.process_name = proc.name() or ""
                    info.executable = proc.exe() or ""
                except Exception:
                    pass
            info.ok = bool(info.title)
        except Exception:
            pass
        return info


__all__ = ["WindowInfo", "WindowProbe"]
