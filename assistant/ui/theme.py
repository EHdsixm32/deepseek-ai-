"""DS娘主题：以 ds 鲸鱼蓝为基底，点缀粉色，界面保持简洁。"""
from __future__ import annotations

PRIMARY = "#5B7CFA"       # DS 蓝
PRIMARY_DARK = "#3E5BDB"
PRIMARY_SOFT = "#E8EDFF"
ACCENT = "#FF9EC7"        # 软糯粉
BG = "#F7F8FF"
CARD = "#FFFFFF"
TEXT = "#24283B"
TEXT_MUTED = "#7A819B"
BORDER = "#DCE2F5"
DANGER = "#E26D8F"

MAIN_QSS = f"""
* {{
    font-family: "Microsoft YaHei UI", "PingFang SC", "Noto Sans CJK SC", sans-serif;
    color: {TEXT};
}}
QWidget {{
    background: {BG};
}}
QMainWindow, QDialog {{
    background: {BG};
}}
QPushButton {{
    background: {CARD};
    border: 1px solid {BORDER};
    border-radius: 9px;
    padding: 7px 14px;
}}
QPushButton:hover {{
    background: {PRIMARY_SOFT};
    border-color: {PRIMARY};
}}
QPushButton#primary {{
    background: {PRIMARY};
    color: white;
    border: none;
    font-weight: 600;
}}
QPushButton#primary:hover {{
    background: {PRIMARY_DARK};
}}
QPushButton#danger {{
    color: {DANGER};
    border-color: #F3C2D2;
}}
QLineEdit, QTextEdit, QPlainTextEdit, QSpinBox, QDoubleSpinBox, QComboBox {{
    background: {CARD};
    border: 1px solid {BORDER};
    border-radius: 8px;
    padding: 6px 8px;
    selection-background-color: {PRIMARY};
}}
QLineEdit:focus, QTextEdit:focus, QPlainTextEdit:focus {{
    border: 1px solid {PRIMARY};
}}
QTabWidget::pane {{
    border: 1px solid {BORDER};
    border-radius: 8px;
    background: {CARD};
}}
QTabBar::tab {{
    background: transparent;
    padding: 8px 16px;
    border-bottom: 2px solid transparent;
}}
QTabBar::tab:selected {{
    color: {PRIMARY};
    border-bottom: 2px solid {PRIMARY};
}}
QScrollArea {{
    border: none;
    background: transparent;
}}
QToolTip {{
    background: {CARD};
    border: 1px solid {BORDER};
    padding: 4px 8px;
}}
"""
