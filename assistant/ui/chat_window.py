"""聊天界面与设置界面。

- 顶部：DS娘头像、名字、状态、目录/设置/新对话/退出程序；
- 中部：消息卡片列表。助手卡片带“思考过程”面板，可折叠/展开；
- 文件工具：读操作按权限直接执行，写/改/删操作弹窗请求用户批准。
"""
from __future__ import annotations

import html
import threading
import time
from pathlib import Path

from ..config import ConfigError
from .theme import ACCENT, BORDER, CARD, MAIN_QSS, PRIMARY, PRIMARY_DARK, TEXT, TEXT_MUTED

try:
    from PySide6.QtCore import QEvent, QObject, QThread, Qt, QTimer, Signal
    from PySide6.QtGui import QFont, QPixmap, QTextCursor
    from PySide6.QtWidgets import (
        QCheckBox, QDialog, QDoubleSpinBox, QFormLayout, QFrame, QHBoxLayout,
        QLabel, QLineEdit, QMainWindow, QMessageBox, QPlainTextEdit, QPushButton, QTextEdit,
        QScrollArea, QSpinBox, QTabWidget, QVBoxLayout, QWidget,
    )
except Exception as _qt_import_error:  # pragma: no cover
    _qt_import_error = _qt_import_error


def _esc(text: str) -> str:
    return html.escape(str(text), quote=False)


def _answer_html(text: str) -> str:
    return _esc(text).replace("\n", "<br>").replace("  ", "&nbsp;&nbsp;")


class ChatThread(QThread):
    turn_event = Signal(object)
    finished_with_result = Signal(dict)
    failed = Signal(str)

    def __init__(self, engine, user_text: str, parent=None):
        super().__init__(parent)
        self.engine = engine
        self.user_text = user_text
        self.full_reply = ""

    def run(self) -> None:
        try:
            for event in self.engine.stream_turn(self.user_text):
                self.turn_event.emit(event)
                if getattr(event, "kind", "") == "answer":
                    self.full_reply = event.text
            result = self.engine.finalize_turn(self.user_text, self.full_reply)
            self.finished_with_result.emit(result)
        except Exception as exc:
            self.failed.emit(str(exc))


class ToolApprovalBridge(QObject):
    approval_requested = Signal(object)


class AssistantMessageCard(QFrame):
    """一条助手回复卡片：最终回答 + 可折叠的思考过程。"""

    def __init__(self, assistant_name: str, parent=None):
        super().__init__(parent)
        self.assistant_name = assistant_name
        self.setObjectName("assistantCard")
        self.setStyleSheet(
            "QFrame#assistantCard { background: #FFFFFF; border: 1px solid #DCE2F5; "
            "border-radius: 12px; }"
        )
        root = QVBoxLayout(self)
        root.setContentsMargins(12, 10, 12, 10)
        root.setSpacing(6)

        header = QHBoxLayout()
        self.status_label = QLabel(f"🐋 {assistant_name} · 正在思考…")
        self.status_label.setStyleSheet(f"color: {TEXT_MUTED}; font-size: 12px; border: none; background: transparent;")
        header.addWidget(self.status_label)
        header.addStretch(1)
        self.think_toggle = QPushButton("🧠 思考过程 ▾")
        self.think_toggle.setCheckable(False)
        self.think_toggle.setStyleSheet(
            f"QPushButton {{ border: none; color: {PRIMARY}; font-size: 12px; background: #EEF1FF; border-radius: 8px; padding: 3px 9px; }}"
            "QPushButton:hover { background: #E2E8FF; }"
        )
        self.think_toggle.clicked.connect(self._toggle_thinking)
        header.addWidget(self.think_toggle)
        root.addLayout(header)

        self.think_panel = QPlainTextEdit()
        self.think_panel.setReadOnly(True)
        self.think_panel.setMaximumHeight(220)
        self.think_panel.setMinimumHeight(70)
        self.think_panel.setStyleSheet(
            f"QPlainTextEdit {{ background: #FAFBFF; color: {TEXT_MUTED}; border: 1px solid {BORDER}; "
            "border-radius: 8px; font-size: 12px; padding: 4px; }}"
        )
        root.addWidget(self.think_panel)
        self.thinking_visible = True

        self.answer_label = QLabel("")
        self.answer_label.setWordWrap(True)
        self.answer_label.setTextFormat(Qt.TextFormat.RichText)
        self.answer_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.answer_label.setStyleSheet(
            f"QLabel {{ color: {TEXT}; font-size: 14px; border: none; background: transparent; padding: 2px; }}"
        )
        root.addWidget(self.answer_label)

    def _toggle_thinking(self) -> None:
        self.thinking_visible = not self.thinking_visible
        self.think_panel.setVisible(self.thinking_visible)
        self.think_toggle.setText("🧠 思考过程 ▾" if self.thinking_visible else "🧠 思考过程 ▸")

    def add_stage(self, text: str) -> None:
        self.think_panel.appendPlainText("◆ " + str(text))
        self.think_panel.verticalScrollBar().setValue(self.think_panel.verticalScrollBar().maximum())

    def add_thinking(self, text: str) -> None:
        self.think_panel.appendPlainText(str(text))
        self.think_panel.verticalScrollBar().setValue(self.think_panel.verticalScrollBar().maximum())

    def add_tool(self, text: str) -> None:
        self.think_panel.appendPlainText("🔧 " + str(text))
        self.think_panel.verticalScrollBar().setValue(self.think_panel.verticalScrollBar().maximum())

    def set_answer(self, text: str) -> None:
        self.answer_label.setText(_answer_html(text or ""))
        self.status_label.setText(f"🐋 {self.assistant_name} · 已回复")
        self.status_label.setStyleSheet(f"color: {PRIMARY}; font-size: 12px; border: none; background: transparent;")

    def set_status(self, text: str) -> None:
        self.status_label.setText(str(text))

    def finish(self) -> None:
        self.set_status(f"🐋 {self.assistant_name} · 已回复")
        if not self.think_panel.toPlainText().strip():
            self.thinking_visible = False
            self.think_panel.setVisible(False)
            self.think_toggle.setText("🧠 思考过程 ▸")


class ChatWindow(QMainWindow):
    settings_requested = Signal()
    memory_requested = Signal()
    quit_requested = Signal()

    def __init__(self, ctx, parent=None):
        super().__init__(parent)
        self.ctx = ctx
        self.engine = ctx.engine
        self.config = ctx.config
        self.memory = ctx.memory
        self._thread: ChatThread | None = None
        self._current_card: AssistantMessageCard | None = None
        self._busy = False
        self._messages: list[QWidget] = []

        self.approval_bridge = ToolApprovalBridge()
        self.approval_bridge.approval_requested.connect(self._show_approval_dialog)
        self.engine.file_tools.approver = self._request_approval

        self.setWindowTitle(f"与 {self.config.assistant_name} 聊天")
        self.resize(int(self.config.get("ui.window_width", 920)),
                    int(self.config.get("ui.window_height", 660)))
        self.setStyleSheet(MAIN_QSS)
        self._build_ui()
        self.append_system(f"你好呀，我是 {self.config.assistant_name}。你可以问我正在做什么、过去做过什么，"
                           "也可以让我直接读取/修改工作区文件。")

    # ---------- UI ----------
    def _build_ui(self) -> None:
        central = QWidget(self)
        root = QVBoxLayout(central)
        root.setContentsMargins(14, 12, 14, 12)
        root.setSpacing(10)

        top = QHBoxLayout()
        avatar = QLabel()
        pix = QPixmap(str(Path(__file__).resolve().parents[2] / "ds娘.png"))
        if pix.isNull():
            avatar.setText("🐋")
            avatar.setStyleSheet("font-size:30px; border:none; background:transparent;")
        else:
            avatar.setPixmap(pix.scaled(42, 42, Qt.AspectRatioMode.KeepAspectRatio,
                                        Qt.TransformationMode.SmoothTransformation))
        avatar.setFixedSize(44, 44)
        top.addWidget(avatar)

        title_box = QVBoxLayout()
        self.name_label = QLabel(self.config.assistant_name)
        self.name_label.setStyleSheet(
            f"font-size:16px; font-weight:700; color:{PRIMARY}; border:none; background:transparent;"
        )
        self.status_label = QLabel("在线 · 可读取/修改工作区文件")
        self.status_label.setStyleSheet(
            f"color:{TEXT_MUTED}; font-size:12px; border:none; background:transparent;"
        )
        title_box.addWidget(self.name_label)
        title_box.addWidget(self.status_label)
        top.addLayout(title_box)
        top.addStretch(1)

        memory_btn = QPushButton("📂 目录文件")
        memory_btn.clicked.connect(self.memory_requested.emit)
        settings_btn = QPushButton("⚙ 设置")
        settings_btn.clicked.connect(self.settings_requested.emit)
        clear_btn = QPushButton("新对话")
        clear_btn.clicked.connect(self._clear_chat)
        exit_btn = QPushButton("⏻ 退出程序")
        exit_btn.setObjectName("danger")
        exit_btn.clicked.connect(self.quit_requested.emit)
        top.addWidget(memory_btn)
        top.addWidget(settings_btn)
        top.addWidget(clear_btn)
        top.addWidget(exit_btn)
        root.addLayout(top)

        # 可滚动消息区
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll.setStyleSheet("QScrollArea { background: transparent; border: none; }")
        self.message_container = QWidget()
        self.message_container.setStyleSheet("background: transparent;")
        self.messages_layout = QVBoxLayout(self.message_container)
        self.messages_layout.setContentsMargins(2, 2, 2, 2)
        self.messages_layout.setSpacing(10)
        self.messages_layout.addStretch(1)
        self.scroll.setWidget(self.message_container)
        root.addWidget(self.scroll, 1)

        # 输入区
        self.input = QTextEdit()
        self.input.setPlaceholderText("输入消息，Ctrl+Enter 发送。例如：帮我看看 README.md，然后加一段说明。")
        self.input.setFixedHeight(78)
        self.send_btn = QPushButton("发送")
        self.send_btn.setObjectName("primary")
        self.send_btn.setMinimumWidth(92)
        self.send_btn.clicked.connect(self.send_message)

        bottom = QHBoxLayout()
        bottom.addWidget(self.input, 1)
        bottom.addWidget(self.send_btn)
        root.addLayout(bottom)

        self.setCentralWidget(central)
        self.input.installEventFilter(self)

    def eventFilter(self, obj, event):  # noqa: N802
        if obj is self.input and event.type() == QEvent.Type.KeyPress:
            if (event.key() == Qt.Key.Key_Return or event.key() == Qt.Key.Key_Enter) and                     event.modifiers() & Qt.KeyboardModifier.ControlModifier:
                self.send_message()
                return True
        return super().eventFilter(obj, event)

    # ---------- 消息卡片 ----------
    def _user_bubble(self, text: str) -> QWidget:
        w = QWidget()
        lay = QHBoxLayout(w)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addStretch(1)
        label = QLabel(_answer_html(text))
        label.setWordWrap(True)
        label.setTextFormat(Qt.TextFormat.RichText)
        label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        label.setStyleSheet(
            f"QLabel {{ background: {PRIMARY}; color: white; border-radius: 14px 14px 4px 14px; "
            "padding: 8px 12px; font-size: 14px; }}"
        )
        label.setMaximumWidth(640)
        lay.addWidget(label)
        return w

    def append_user(self, text: str) -> None:
        self._append_widget(self._user_bubble(text))

    def append_system(self, text: str) -> None:
        label = QLabel(_answer_html(text))
        label.setWordWrap(True)
        label.setTextFormat(Qt.TextFormat.RichText)
        label.setStyleSheet(f"color: {TEXT_MUTED}; font-size: 12px; border: none; background: transparent;")
        self._append_widget(label)

    def _append_widget(self, widget: QWidget) -> None:
        self.messages_layout.insertWidget(self.messages_layout.count() - 1, widget)
        self._messages.append(widget)
        self._scroll_to_bottom()

    def _scroll_to_bottom(self) -> None:
        def scroll():
            bar = self.scroll.verticalScrollBar()
            bar.setValue(bar.maximum())
        QTimer.singleShot(0, scroll)

    def _clear_chat(self) -> None:
        if self._busy:
            return
        self.engine.reset_conversation()
        while self.messages_layout.count() > 1:
            item = self.messages_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        self._messages.clear()
        self.append_system("新对话已开始。")

    # ---------- 发送与接收 ----------
    def send_message(self) -> None:
        if self._busy:
            return
        text = self.input.toPlainText().strip()
        if not text:
            return
        if not self.engine.harness.is_configured():
            self.append_system("还没有配置 DeepSeek API Key。请点击右上角“设置”，在 DeepSeek 页填写 Key。")
            return
        self.append_user(text)
        self.input.clear()
        self._busy = True
        self.send_btn.setEnabled(False)
        self.send_btn.setText("思考中…")

        card = AssistantMessageCard(self.config.assistant_name)
        self._append_widget(card)
        self._current_card = card

        self._thread = ChatThread(self.engine, text, self)
        self._thread.turn_event.connect(self._on_turn_event)
        self._thread.finished_with_result.connect(self._on_finished)
        self._thread.failed.connect(self._on_failed)
        self._thread.start()

    def _on_turn_event(self, event) -> None:
        if self._current_card is None:
            return
        kind = getattr(event, "kind", "")
        text = getattr(event, "text", "")
        if kind == "stage":
            self._current_card.add_stage(text)
        elif kind == "thinking":
            self._current_card.add_thinking(text)
        elif kind == "tool":
            self._current_card.add_tool(text)
        elif kind == "answer":
            self._current_card.set_answer(text)
        self._scroll_to_bottom()

    def _on_finished(self, result: dict) -> None:
        if self._current_card is not None:
            self._current_card.finish()
        judge = result.get("judge", {})
        entry = result.get("entry")
        if entry is not None:
            self.append_system(
                f"📝 这次对话已写入长期记忆：{entry.id}｜{entry.topic}（重要度 {judge.get('importance', 0):.2f}）"
            )
        else:
            self.append_system(
                f"本次对话未写入长期记忆（重要度 {judge.get('importance', 0):.2f}，阈值 {judge.get('threshold', 0):.2f}）"
            )
        self._current_card = None
        self._reset_input_state()
        self._thread = None

    def _on_failed(self, error: str) -> None:
        if self._current_card is not None:
            self._current_card.set_status(f"⚠ {error}")
        self.append_system(f"⚠ 出错：{error}")
        self._current_card = None
        self._reset_input_state()
        self._thread = None

    def _reset_input_state(self) -> None:
        self._busy = False
        self.send_btn.setEnabled(True)
        self.send_btn.setText("发送")
        self.input.setFocus()

    # ---------- 工具权限批准（写/改/删） ----------
    def _request_approval(self, tool_name: str, arguments: dict, risk: str, path: str):
        request = {
            "tool_name": tool_name,
            "arguments": arguments,
            "risk": risk,
            "path": path,
            "approved": False,
            "reason": "",
            "event": threading.Event(),
        }
        self.approval_bridge.approval_requested.emit(request)
        request["event"].wait(timeout=90)
        return bool(request["approved"]), str(request.get("reason") or "")

    def _show_approval_dialog(self, request: dict) -> None:
        try:
            args_text = str(request.get("arguments") or {})
            if len(args_text) > 700:
                args_text = args_text[:700] + "…"
            msg = QMessageBox(self)
            msg.setWindowTitle("文件工具权限确认")
            msg.setIcon(QMessageBox.Icon.Question)
            msg.setText(f"助手请求执行文件操作：{request.get('tool_name')}")
            msg.setInformativeText(
                f"操作类型：{request.get('risk')}\n"
                f"目标路径：{request.get('path')}\n"
                f"参数：{args_text}\n\n是否允许？"
            )
            allow = msg.addButton("允许", QMessageBox.ButtonRole.AcceptRole)
            deny = msg.addButton("拒绝", QMessageBox.ButtonRole.RejectRole)
            msg.exec()
            request["approved"] = msg.clickedButton() is allow
            request["reason"] = "用户在弹窗中确认" if request["approved"] else "用户在弹窗中拒绝"
        finally:
            request["event"].set()

    def closeEvent(self, event) -> None:  # noqa: N802
        if self._thread is not None and self._thread.isRunning():
            self._thread.wait(2500)
        super().closeEvent(event)


class SettingsDialog(QDialog):
    """参数调整面板：范围校验交给 ConfigManager，不会让程序崩溃。"""
    config_changed = Signal(list)

    def __init__(self, ctx, parent=None):
        super().__init__(parent)
        self.ctx = ctx
        self.config = ctx.config
        self.setWindowTitle("助手设置")
        self.setMinimumWidth(640)
        self.setStyleSheet(MAIN_QSS)
        self._fields: dict[str, object] = {}
        self._build_ui()

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        tabs = QTabWidget()
        tabs.addTab(self._persona_tab(), "助手")
        tabs.addTab(self._deepseek_tab(), "DeepSeek")
        tabs.addTab(self._memory_tab(), "记忆与判定")
        tabs.addTab(self._tools_tab(), "文件工具权限")
        tabs.addTab(self._monitor_tab(), "活动监视")
        tabs.addTab(self._pet_tab(), "桌宠")
        root.addWidget(tabs)

        note = QLabel("保存后所有改动都会写入“目录文件”和对应的“目的文件”。")
        note.setStyleSheet(f"color:{TEXT_MUTED}; border:none; background:transparent;")
        root.addWidget(note)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        cancel = QPushButton("取消")
        cancel.clicked.connect(self.reject)
        ok = QPushButton("保存")
        ok.setObjectName("primary")
        ok.clicked.connect(self._save)
        buttons.addWidget(cancel)
        buttons.addWidget(ok)
        root.addLayout(buttons)

    def _wrap_tab(self, form: QFormLayout) -> QWidget:
        w = QWidget()
        w.setLayout(form)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(w)
        return scroll

    def _form(self) -> QFormLayout:
        form = QFormLayout()
        form.setContentsMargins(16, 16, 16, 16)
        form.setSpacing(10)
        return form

    def _add_text(self, form, path, label, placeholder="") -> None:
        edit = QLineEdit(str(self.config.get(path, "")))
        edit.setPlaceholderText(placeholder)
        form.addRow(label, edit)
        self._fields[path] = edit

    def _add_spin(self, form, path, label, lo, hi, decimals=0) -> None:
        if decimals:
            box = QDoubleSpinBox()
            box.setDecimals(decimals)
            box.setSingleStep(0.05)
        else:
            box = QSpinBox()
        box.setRange(lo, hi)
        box.setValue(float(self.config.get(path, lo)))
        form.addRow(label, box)
        self._fields[path] = box

    def _add_bool(self, form, path, label) -> None:
        box = QCheckBox()
        box.setChecked(bool(self.config.get(path, False)))
        form.addRow(label, box)
        self._fields[path] = box

    def _persona_tab(self) -> QWidget:
        form = self._form()
        self._add_text(form, "assistant_name", "助手名字", "例如：小鲸")
        self._add_text(form, "persona", "人设 / 语气", "例如：软糯可爱温柔可靠")
        self._add_bool(form, "ui.show_memory_panel", "聊天窗口显示记忆条数")
        return self._wrap_tab(form)

    def _deepseek_tab(self) -> QWidget:
        form = self._form()
        self._add_text(form, "deepseek.api_key", "API Key", "sk-... 或留空使用环境变量 DEEPSEEK_API_KEY")
        self._fields["deepseek.api_key"].setEchoMode(QLineEdit.EchoMode.Password)
        self._add_text(form, "deepseek.base_url", "Base URL", "https://api.deepseek.com/v1")
        self._add_text(form, "deepseek.model", "模型", "deepseek-chat / deepseek-reasoner")
        self._add_spin(form, "deepseek.temperature", "温度", 0.0, 2.0, 2)
        self._add_spin(form, "deepseek.max_tokens", "最大输出 token", 64, 32768)
        self._add_spin(form, "deepseek.timeout_seconds", "超时（秒）", 5, 600)
        return self._wrap_tab(form)

    def _memory_tab(self) -> QWidget:
        form = self._form()
        self._add_spin(form, "memory.max_directory_entries", "目录文件最大读取条数", 1, 2000)
        self._add_spin(form, "memory.max_selected_purposes", "每次最多打开目的文件数", 1, 20)
        self._add_spin(form, "memory.max_purpose_chars", "目的文件最大读入字符", 200, 200000)
        self._add_spin(form, "memory.decay_half_life_days", "权重时间半衰期（天）", 0.5, 3650, 1)
        self._add_spin(form, "judge.threshold", "写入记忆的重要性阈值", 0.0, 1.0, 2)
        self._add_bool(form, "judge.use_ai", "判定器接入 DeepSeek 语义评分")
        return self._wrap_tab(form)

    def _tools_tab(self) -> QWidget:
        form = self._form()
        self._add_bool(form, "tools.enabled", "启用本地文件工具")
        self._add_text(form, "tools.workspace_root", "工作区根目录", "默认 . 表示本程序目录")
        self._add_bool(form, "tools.allow_read", "允许读取文件")
        self._add_bool(form, "tools.allow_write", "允许创建/覆盖文件")
        self._add_bool(form, "tools.allow_edit", "允许精确修改文件")
        self._add_bool(form, "tools.allow_delete", "允许删除文件")
        self._add_bool(form, "tools.allow_list", "允许列出目录")
        self._add_bool(form, "tools.allow_outside_workspace", "允许访问工作区外路径（危险）")
        self._add_bool(form, "tools.deny_sensitive_files", "禁止访问配置/密钥/凭据文件（推荐开启）")
        self._add_bool(form, "tools.auto_approve", "写/改/删操作免确认（自动批准）")
        self._add_spin(form, "tools.max_file_bytes", "单文件大小上限（字节）", 1024, 50000000)
        self._add_spin(form, "tools.max_tool_rounds", "单次最多工具调用轮数", 1, 20)
        return self._wrap_tab(form)

    def _monitor_tab(self) -> QWidget:
        form = self._form()
        self._add_bool(form, "activity_monitor.enabled", "启用活动监视")
        self._add_bool(form, "activity_monitor.window_title_enabled", "读取前台窗口标题")
        self._add_bool(form, "activity_monitor.process_snapshot_enabled", "读取任务管理器进程快照")
        self._add_bool(form, "activity_monitor.browser_history_enabled", "读取浏览器历史（需谨慎）")
        self._add_spin(form, "activity_monitor.interval_seconds", "采样间隔（秒）", 2, 3600)
        self._add_spin(form, "activity_monitor.session_min_seconds", "最短会话时长（秒）", 5, 86400)
        self._add_spin(form, "activity_monitor.flush_after_seconds", "会话自动分段（秒）", 60, 86400)
        return self._wrap_tab(form)

    def _pet_tab(self) -> QWidget:
        form = self._form()
        self._add_spin(form, "pet.size", "桌宠尺寸（像素）", 48, 600)
        self._add_spin(form, "pet.opacity", "不透明度", 0.3, 1.0, 2)
        self._add_spin(form, "pet.click_count_to_open", "连续点击几次打开聊天", 1, 5)
        self._add_spin(form, "pet.click_interval_ms", "连续点击判定间隔（毫秒）", 200, 3000)
        self._add_bool(form, "pet.always_on_top", "桌宠始终置顶")
        self._add_bool(form, "pet.animation", "轻微浮动动画")
        return self._wrap_tab(form)

    def _save(self) -> None:
        changes: dict[str, object] = {}
        for path, widget in self._fields.items():
            current = self.config.get(path)
            if isinstance(widget, QCheckBox):
                value = widget.isChecked()
            elif isinstance(widget, (QSpinBox, QDoubleSpinBox)):
                value = widget.value()
            else:
                value = widget.text().strip()
            if value != current:
                changes[path] = value
        if not changes:
            self.accept()
            return
        try:
            applied = self.config.update_many(changes)
        except ConfigError as exc:
            QMessageBox.warning(self, "参数未保存", f"参数不合法，已取消本次保存：\n{exc}")
            return
        try:
            self.ctx.memory.record_config_change(applied)
            memory_note = "设置已生效，并已写入目录文件和目的文件。"
        except Exception as exc:
            memory_note = f"设置已生效，但写入记忆失败：{exc}"
        self.config_changed.emit(applied)
        QMessageBox.information(self, "已保存", memory_note)
        self.accept()
