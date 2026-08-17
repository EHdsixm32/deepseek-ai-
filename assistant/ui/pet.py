"""DS娘桌宠。

- 使用 ds娘.png 作为形象；
- 无边框、透明、置顶，可用鼠标随心拖动；
- 在设定时间窗内连续点击（默认 2 次，可调）打开聊天界面；
- 右键菜单可打开聊天、设置、目录文件，或退出。
"""
from __future__ import annotations

import datetime as dt
from pathlib import Path

from ..config import PET_IMAGE
from .theme import PRIMARY, PRIMARY_SOFT

try:
    from PySide6.QtCore import QPoint, Qt, QTimer, Signal
    from PySide6.QtGui import QAction, QColor, QCursor, QPainter, QPixmap
    from PySide6.QtWidgets import QLabel, QMenu, QVBoxLayout, QWidget
except Exception as _qt_import_error:  # pragma: no cover
    _qt_import_error = _qt_import_error


class Pet(QWidget):
    chat_requested = Signal()
    settings_requested = Signal()
    memory_requested = Signal()
    quit_requested = Signal()

    def __init__(self, config: "object", parent=None):  # noqa: F821
        super().__init__(parent, Qt.WindowType.FramelessWindowHint | Qt.WindowType.Tool)
        self.config = config
        self.name = config.assistant_name
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setWindowTitle(f"{self.name} · 桌宠")
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, bool(config.get("pet.always_on_top", True)))
        self._drag_pos: QPoint | None = None
        self._moved = False
        self._base_pos: QPoint | None = None
        self._clicks: list[dt.datetime] = []
        self._bob_offset = 0
        self._bob_up = True

        self.image_label = QLabel(self)
        self.image_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.name_label = QLabel(self.name, self)
        self.name_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.name_label.setStyleSheet(
            f"background: rgba(255,255,255,215); color: {PRIMARY}; border-radius: 10px; "
            f"padding: 2px 10px; font-size: 11px; font-weight: 600;"
        )
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self.image_label)
        layout.addWidget(self.name_label)

        self.setToolTip(f"拖动可以移动 {self.name}；连续点击打开聊天")
        self._anim = QTimer(self)
        self._anim.timeout.connect(self._animate)
        self._load_pixmap()
        self.apply_config()

    def _load_pixmap(self) -> None:
        image = Path(PET_IMAGE)
        size = int(self.config.get("pet.size", 170))
        if image.is_file():
            pixmap = QPixmap(str(image))
            if not pixmap.isNull():
                self._pixmap = pixmap.scaled(
                    size, size, Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
                self._fallback = False
                return
        self._fallback = True
        self._pixmap = self._fallback_pixmap(size)

    def _fallback_pixmap(self, size: int) -> QPixmap:
        pm = QPixmap(size, size)
        pm.fill(Qt.GlobalColor.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setBrush(QColor(PRIMARY_SOFT))
        p.setPen(QColor(PRIMARY))
        p.drawEllipse(8, 14, size - 16, size - 22)
        p.setBrush(QColor(PRIMARY))
        p.drawEllipse(int(size * 0.34), int(size * 0.44), int(size * 0.1), int(size * 0.12))
        p.drawEllipse(int(size * 0.58), int(size * 0.44), int(size * 0.1), int(size * 0.12))
        p.end()
        return pm

    def apply_config(self) -> None:
        size = int(self.config.get("pet.size", 170))
        self._load_pixmap()
        self.image_label.setPixmap(self._pixmap)
        self.image_label.setFixedSize(self._pixmap.width() + 8, self._pixmap.height() + 8)
        self.name = self.config.assistant_name
        self.name_label.setText(self.name)
        self.setWindowOpacity(float(self.config.get("pet.opacity", 0.98)))
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, bool(self.config.get("pet.always_on_top", True)))
        self.setWindowTitle(f"{self.name} · 桌宠")
        self.setToolTip(f"拖动可以移动 {self.name}；连续点击打开聊天")
        self.adjustSize()
        if bool(self.config.get("pet.animation", True)) and not self._anim.isActive():
            self._anim.start(90)
        elif not bool(self.config.get("pet.animation", True)):
            self._anim.stop()
        # 修改置顶等窗口标志后，Qt 可能隐藏窗口；如原来可见则恢复
        if self.isVisible():
            self.show()

    def _animate(self) -> None:
        if self._drag_pos is not None:
            return
        if self._base_pos is None:
            self._base_pos = self.pos()
        self._bob_offset = 2 if self._bob_up else 0
        self._bob_up = not self._bob_up
        self.move(self._base_pos.x(), self._base_pos.y() - self._bob_offset)

    # ---------- 拖动与点击 ----------
    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_pos = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            self._moved = False
            event.accept()

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if self._drag_pos is not None and event.buttons() & Qt.MouseButton.LeftButton:
            target = event.globalPosition().toPoint() - self._drag_pos
            if (target - self.pos()).manhattanLength() > 2:
                self._moved = True
            self.move(target)
            self._base_pos = target
            event.accept()

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            was_drag = self._moved
            self._drag_pos = None
            if not was_drag:
                self._register_click()
            event.accept()

    def _register_click(self) -> None:
        now = dt.datetime.now()
        interval = int(self.config.get("pet.click_interval_ms", 500))
        threshold = self._clicks and ((now - self._clicks[-1]).total_seconds() * 1000 <= interval)
        if not threshold:
            self._clicks.clear()
        self._clicks.append(now)
        needed = int(self.config.get("pet.click_count_to_open", 2))
        if len(self._clicks) >= needed:
            self._clicks.clear()
            self.chat_requested.emit()

    def contextMenuEvent(self, event) -> None:  # noqa: N802
        menu = QMenu(self)
        chat_action = QAction(f"和 {self.name} 聊天", self)
        chat_action.triggered.connect(self.chat_requested.emit)
        settings_action = QAction("设置", self)
        settings_action.triggered.connect(self.settings_requested.emit)
        memory_action = QAction("打开目录文件", self)
        memory_action.triggered.connect(self.memory_requested.emit)
        quit_action = QAction("退出桌宠", self)
        quit_action.triggered.connect(self.quit_requested.emit)
        menu.addAction(chat_action)
        menu.addAction(settings_action)
        menu.addAction(memory_action)
        menu.addSeparator()
        menu.addAction(quit_action)
        menu.exec(event.globalPosition().toPoint())
