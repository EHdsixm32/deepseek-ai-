#!/usr/bin/env python3
"""DS娘 AI 智能助手入口。

用法：
    python run.py                # 启动桌宠 + 聊天界面（需要 PySide6）
    python run.py chat           # 无 GUI 的命令行聊天（需要 DEEPSEEK_API_KEY）
    python run.py memory list    # 查看目录文件条目
    python run.py doctor         # 检查项目状态

也可以直接双击“启动DS娘.bat”或“启动DS娘.vbs”一键启动。
"""
import sys
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from assistant.app import run_cli  # noqa: E402
from assistant.desktop import launch_gui  # noqa: E402


def _show_messagebox(title: str, text: str) -> None:
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(0, text, title, 0x10)
    except Exception:
        pass


def main() -> int:
    if "--no-gui" in sys.argv:
        sys.argv.remove("--no-gui")
        return run_cli()
    if len(sys.argv) > 1 and sys.argv[1] in ("chat", "memory", "doctor"):
        return run_cli()
    return launch_gui()


if __name__ == "__main__":
    code = 1
    try:
        code = main()
    except Exception as exc:
        log_dir = Path(__file__).resolve().parent / "data"
        try:
            log_dir.mkdir(parents=True, exist_ok=True)
            log_file = log_dir / "启动错误.log"
            with log_file.open("a", encoding="utf-8") as fh:
                fh.write("\n" + "=" * 60 + "\n")
                traceback.print_exc(file=fh)
        except Exception:
            log_file = None
        detail = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        if sys.stdout is None or sys.stderr is None:
            _show_messagebox("DS娘助手启动失败", f"{exc}\n\n日志：{getattr(log_file, 'name', '未知')}")
        else:
            print(detail, file=sys.stderr)
        code = 1
    raise SystemExit(code)
