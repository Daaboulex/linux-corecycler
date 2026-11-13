from __future__ import annotations

import pytest
from PySide6.QtDBus import QDBusConnection, QDBusVariant

from corecycler.monitor import power_profile as pp


@pytest.mark.req("T22.F2")
def test_the_profile_is_the_daemon_s_reply_and_a_failed_read_says_unreadable():
    question = pp.ask()
    assert pp.profile_from(question.createReply(QDBusVariant("balanced"))) == "balanced"
    assert pp.profile_from(question.createErrorReply(pp.ABSENT, "no daemon")) is None
    denied = question.createErrorReply("org.freedesktop.DBus.Error.AccessDenied", "denied")
    assert pp.profile_from(denied) == "unreadable"
    assert pp.profile_from(question.createReply()) == "unreadable"
    assert pp.profile_from(question.createReply(QDBusVariant(3))) == "unreadable"
    assert pp.profile_from(question.createReply("balanced")) == "unreadable"


@pytest.mark.req("T22.F2")
def test_a_bus_that_cannot_be_reached_reads_unreadable():
    bus = QDBusConnection.connectToBus("unix:path=/nonexistent/corecycler-test-bus", "corecycler-test-bus")
    assert not bus.isConnected()
    assert pp.power_profile(bus) == "unreadable"
