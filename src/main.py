"""Linux CoreCycler — Per-core CPU stability tester and PBO Curve Optimizer tuner."""

from __future__ import annotations

import sys
from pathlib import Path

# add src to path for direct execution
src_dir = Path(__file__).parent
if str(src_dir) not in sys.path:
    sys.path.insert(0, str(src_dir))


def main() -> int:
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication

    # high DPI support
    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )

    app = QApplication(sys.argv)
    app.setApplicationName("Linux CoreCycler")
    app.setOrganizationName("linux-corecycler")

    # app icon
    from PySide6.QtGui import QIcon

    icon_path = Path(__file__).parent.parent / "assets" / "icon.svg"
    if icon_path.exists():
        app.setWindowIcon(QIcon(str(icon_path)))

    # dark theme
    app.setStyleSheet(_dark_stylesheet())

    from gui.main_window import MainWindow

    window = MainWindow()
    window.show()

    return app.exec()


def _dark_stylesheet() -> str:
    return """
        QMainWindow, QWidget {
            background-color: #1e1e1e;
            color: #ddd;
        }
        QTabWidget::pane {
            border: 1px solid #333;
            background: #1e1e1e;
        }
        QTabBar::tab {
            background: #2d2d2d;
            color: #aaa;
            padding: 8px 16px;
            border: 1px solid #333;
            border-bottom: none;
            border-top-left-radius: 4px;
            border-top-right-radius: 4px;
        }
        QTabBar::tab:selected {
            background: #1e1e1e;
            color: #fff;
        }
        QTabBar::tab:hover {
            background: #353535;
        }
        QGroupBox {
            border: 1px solid #333;
            border-radius: 4px;
            margin-top: 12px;
            padding-top: 12px;
            font-weight: bold;
            color: #aaa;
        }
        QGroupBox::title {
            subcontrol-origin: margin;
            left: 10px;
            padding: 0 4px;
        }
        QTableWidget {
            background-color: #252525;
            alternate-background-color: #2a2a2a;
            gridline-color: #333;
            border: 1px solid #333;
            color: #ddd;
        }
        QTableWidget::item:selected {
            background-color: #1a3a5c;
        }
        QHeaderView::section {
            background-color: #2d2d2d;
            color: #aaa;
            padding: 4px;
            border: 1px solid #333;
            font-weight: bold;
        }
        QComboBox, QSpinBox, QLineEdit {
            background-color: #2d2d2d;
            color: #ddd;
            border: 1px solid #444;
            border-radius: 3px;
            padding: 4px 8px;
        }
        QComboBox:focus, QSpinBox:focus, QLineEdit:focus {
            border-color: #4fc3f7;
        }
        QComboBox::drop-down {
            border: none;
            width: 24px;
            subcontrol-position: right center;
        }
        QComboBox::down-arrow {
            image: none;
            border-left: 5px solid transparent;
            border-right: 5px solid transparent;
            border-top: 6px solid #888;
            margin-right: 6px;
        }
        QComboBox::down-arrow:hover {
            border-top-color: #4fc3f7;
        }
        QSpinBox::up-button, QDoubleSpinBox::up-button {
            subcontrol-position: top right;
            border: none;
            border-left: 1px solid #444;
            width: 20px;
        }
        QSpinBox::down-button, QDoubleSpinBox::down-button {
            subcontrol-position: bottom right;
            border: none;
            border-left: 1px solid #444;
            width: 20px;
        }
        QSpinBox::up-arrow, QDoubleSpinBox::up-arrow {
            image: none;
            border-left: 4px solid transparent;
            border-right: 4px solid transparent;
            border-bottom: 5px solid #888;
        }
        QSpinBox::down-arrow, QDoubleSpinBox::down-arrow {
            image: none;
            border-left: 4px solid transparent;
            border-right: 4px solid transparent;
            border-top: 5px solid #888;
        }
        QSpinBox::up-arrow:hover, QDoubleSpinBox::up-arrow:hover {
            border-bottom-color: #4fc3f7;
        }
        QSpinBox::down-arrow:hover, QDoubleSpinBox::down-arrow:hover {
            border-top-color: #4fc3f7;
        }
        QSpinBox::up-button:hover, QSpinBox::down-button:hover,
        QDoubleSpinBox::up-button:hover, QDoubleSpinBox::down-button:hover {
            background: #353535;
        }
        QPushButton {
            background-color: #2d2d2d;
            color: #ddd;
            border: 1px solid #444;
            border-radius: 4px;
            padding: 6px 12px;
        }
        QPushButton:hover {
            background-color: #353535;
        }
        QPushButton:pressed {
            background-color: #1a1a1a;
        }
        QPushButton:disabled {
            color: #555;
            background-color: #222;
        }
        QCheckBox {
            color: #ddd;
            spacing: 8px;
        }
        QCheckBox::indicator {
            width: 16px;
            height: 16px;
        }
        QPlainTextEdit {
            background-color: #1a1a1a;
            color: #ddd;
            border: 1px solid #333;
        }
        QScrollBar:vertical {
            background: #1e1e1e;
            width: 10px;
        }
        QScrollBar::handle:vertical {
            background: #444;
            border-radius: 5px;
            min-height: 20px;
        }
        QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
            height: 0;
        }
        QStatusBar {
            background: #252525;
            color: #aaa;
            border-top: 1px solid #333;
        }
        QToolBar {
            background: #252525;
            border-bottom: 1px solid #333;
            spacing: 8px;
            padding: 4px;
        }
        QLabel {
            color: #ddd;
        }
        QScrollArea {
            border: none;
        }
        QSplitter::handle {
            background: #333;
            height: 2px;
        }
    """


if __name__ == "__main__":
    sys.exit(main())
