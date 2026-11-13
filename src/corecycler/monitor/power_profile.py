"""The power-profiles-daemon profile, read over the system bus."""

from __future__ import annotations

from PySide6.QtDBus import QDBus, QDBusConnection, QDBusMessage, QDBusVariant

SERVICE = "org.freedesktop.UPower.PowerProfiles"
PATH = "/org/freedesktop/UPower/PowerProfiles"
ABSENT = "org.freedesktop.DBus.Error.ServiceUnknown"
UNREADABLE = "unreadable"
TIMEOUT_MS = 250  # equivalent-mutant: any short bound keeps a stuck daemon from stalling the GUI tick


def ask() -> QDBusMessage:
    message = QDBusMessage.createMethodCall(SERVICE, PATH, "org.freedesktop.DBus.Properties", "Get")
    message.setArguments([SERVICE, "ActiveProfile"])
    return message


def profile_from(reply: QDBusMessage) -> str | None:
    if reply.type() is QDBusMessage.MessageType.ErrorMessage:
        return None if reply.errorName() == ABSENT else UNREADABLE
    values = reply.arguments()
    if len(values) != 1 or not isinstance(values[0], QDBusVariant) or not isinstance(values[0].variant(), str):
        return UNREADABLE
    return values[0].variant()


def power_profile(bus: QDBusConnection | None = None) -> str | None:
    connection = QDBusConnection.systemBus() if bus is None else bus
    return profile_from(connection.call(ask(), QDBus.CallMode.Block, TIMEOUT_MS))
