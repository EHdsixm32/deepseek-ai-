"""GUI 启动：DS娘桌宠 + 聊天窗口 + 设置 + 系统托盘。

若 PySide6 未安装，会给出友好安装提示，而不是崩溃。
"""
from __future__ import annotations

import os
import subprocess
import sys

from assistant.app import AppContext, build_context, mark_config_recorded, record_work_session
from assistant.config import APP_DIR
from assistant.ui.theme import MAIN_QSS


def open_directory_file(ctx: AppContext) -> None:
    path = ctx.memory.directory_file
    try:
        if sys.platform.startswith("win"):
            os.startfile(path)  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(path)])
        else:
            subprocess.Popen(["xdg-open", str(path)])
    except Exception:
        pass


def launch_gui() -> int:
    try:
        from PySide6.QtCore import Qt, QTimer
        from PySide6.QtGui import QIcon, QAction
        from PySide6.QtWidgets import QApplication, QSystemTrayIcon, QMenu
    except Exception as exc:
        msg = "未检测到 PySide6。请先安装：pip install -r requirements.txt\n" + str(exc)
        if sys.stdout is None or sys.stderr is None:
            try:
                import ctypes
                ctypes.windll.user32.MessageBoxW(0, msg, "DS娘助手启动失败", 0x10)
            except Exception:
                pass
        else:
            print(msg)
        return 3

    ctx = build_context()
    app = QApplication(sys.argv)
    app.setApplicationName(f"{ctx.config.assistant_name} AI 助手")
    app.setStyleSheet(MAIN_QSS)
    # 关闭聊天窗口只隐藏它，桌宠继续留在桌面
    app.setQuitOnLastWindowClosed(False)

    from assistant.ui.pet import Pet
    from assistant.ui.chat_window import ChatWindow, SettingsDialog

    pet = Pet(ctx.config)
    pet.move(int(ctx.config.get("pet.size", 170)) + 40, 180)
    pet.show()

    chat = ChatWindow(ctx)

    settings_dialog: SettingsDialog | None = None

    def show_chat() -> None:
        chat.show()
        chat.raise_()
        chat.activateWindow()

    def show_settings() -> None:
        nonlocal settings_dialog
        settings_dialog = SettingsDialog(ctx, chat)
        def after_changes(changes):  # noqa: ANN001
            # 记忆写入已在 SettingsDialog 内完成，这里只负责让 GUI 立即生效
            mark_config_recorded(ctx)
            pet.apply_config()
            chat.name_label.setText(ctx.config.assistant_name)
            chat.setWindowTitle(f"与 {ctx.config.assistant_name} 聊天")
            tools_on = bool(ctx.config.get("tools.enabled", True))
            chat.status_label.setText("在线 · 文件工具已开启" if tools_on else "在线 · 文件工具已关闭")
            ctx.memory.set_threshold(float(ctx.config.get("memory.default_min_importance", 0.45)))
            ctx.judge.threshold = float(ctx.config.get("judge.threshold", 0.45))
            ctx.judge.use_ai = bool(ctx.config.get("judge.use_ai", False))
            ctx.judge.recency_half_life = float(ctx.config.get("judge.recency_half_life_days", 14.0))
            # 活动监视器：刷新采样参数，并按新开关启动/停止
            ctx.monitor.interval = max(2, int(ctx.config.get("activity_monitor.interval_seconds", 6)))
            ctx.monitor.browser_enabled = bool(ctx.config.get("activity_monitor.browser_history_enabled", False))
            ctx.monitor.window_enabled = bool(ctx.config.get("activity_monitor.window_title_enabled", True))
            ctx.monitor.process_enabled = bool(ctx.config.get("activity_monitor.process_snapshot_enabled", True))
            enabled = bool(ctx.config.get("activity_monitor.enabled", True))
            ctx.monitor.enabled = enabled
            if enabled and not ctx.monitor.is_alive():
                try:
                    ctx.monitor.start()
                except RuntimeError:
                    from assistant.monitor import ActivityMonitor
                    ctx.monitor = ActivityMonitor(ctx.config)
                    ctx.monitor.on_session = lambda session: record_work_session(ctx, session)
                    ctx.engine.monitor = ctx.monitor
                    ctx.monitor.start()
            elif not enabled and ctx.monitor.is_alive():
                ctx.monitor.stop()
        settings_dialog.config_changed.connect(after_changes)
        settings_dialog.show()

    def show_memory() -> None:
        open_directory_file(ctx)

    def quit_app() -> None:
        ctx.monitor.stop()
        if ctx.monitor.is_alive():
            ctx.monitor.join(timeout=2)
        app.quit()

    pet.chat_requested.connect(show_chat)
    pet.settings_requested.connect(show_settings)
    pet.memory_requested.connect(show_memory)
    pet.quit_requested.connect(quit_app)
    chat.settings_requested.connect(show_settings)
    chat.memory_requested.connect(show_memory)
    chat.quit_requested.connect(quit_app)

    # 托盘：即使关闭聊天窗口，桌宠仍然在线
    tray = QSystemTrayIcon(QIcon(str(APP_DIR / "ds娘.png")), app)
    tray.setToolTip(f"{ctx.config.assistant_name} · AI 助手")
    menu = QMenu()
    a_chat = QAction(f"和 {ctx.config.assistant_name} 聊天", menu)
    a_chat.triggered.connect(show_chat)
    a_settings = QAction("设置", menu)
    a_settings.triggered.connect(show_settings)
    a_memory = QAction("打开目录文件", menu)
    a_memory.triggered.connect(show_memory)
    a_quit = QAction("退出", menu)
    a_quit.triggered.connect(quit_app)
    menu.addAction(a_chat)
    menu.addAction(a_settings)
    menu.addAction(a_memory)
    menu.addSeparator()
    menu.addAction(a_quit)
    tray.setContextMenu(menu)
    tray.activated.connect(lambda reason: show_chat() if reason == QSystemTrayIcon.ActivationReason.Trigger else None)
    tray.show()

    # 按用户配置启动活动监视器
    if ctx.monitor.enabled:
        ctx.monitor.start()
    QTimer.singleShot(600, lambda: chat.show() if ctx.config.get("ui.auto_open_chat", False) else None)
    app.aboutToQuit.connect(lambda: (ctx.monitor.stop(), ctx.monitor.join(timeout=2)))

    return app.exec()
