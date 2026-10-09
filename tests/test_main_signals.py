from __future__ import annotations

import signal

import pytest
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication, QWidget

from corecycler.main import _install_signal_handlers

STOPS = (signal.SIGTERM, signal.SIGINT, signal.SIGHUP)


@pytest.fixture
def qapp():
    yield QApplication.instance() or QApplication([])


@pytest.fixture
def restored_handlers():
    previous = {number: signal.getsignal(number) for number in STOPS}
    yield
    for number, handler in previous.items():
        signal.signal(number, handler)


class Window(QWidget):
    def __init__(self) -> None:
        super().__init__()
        self.database_open = True

    def closeEvent(self, event) -> None:
        self.database_open = False
        event.accept()


class App:
    def __init__(self, window: Window, qapp: QApplication) -> None:
        self.window = window
        self.qapp = qapp

    def quit(self) -> None:
        self.window.close()
        self.qapp.exit()


@pytest.mark.req("GUI.SIGNAL_QUIT")
@pytest.mark.parametrize("number", STOPS)
def test_a_stop_signal_during_a_slot_closes_the_window_after_the_slot(qapp, restored_handlers, number):
    window = Window()
    window.show()
    cleaned: list[bool] = []
    seen_by_slot: list[bool] = []
    _install_signal_handlers(App(window, qapp), lambda: cleaned.append(window.database_open))

    def tick() -> None:
        signal.raise_signal(number)
        seen_by_slot.append(window.database_open)

    deadline = QTimer()
    deadline.setSingleShot(True)
    deadline.timeout.connect(qapp.exit)
    deadline.start(5000)
    QTimer.singleShot(0, tick)
    qapp.exec()
    deadline.stop()

    assert seen_by_slot == [True]
    assert cleaned == [True]
    assert window.database_open is False
