"""Stress test backend registry: load_all imports each backend module, which registers itself."""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from corecycler.engine.backends.base import StressBackend

BACKEND_REGISTRY: dict[str, type[StressBackend]] = {}


def register_backend(name: str):
    """Decorator to register a backend class by display name."""

    def decorator(cls):
        BACKEND_REGISTRY[name] = cls
        return cls

    return decorator


def get_backend(name: str) -> StressBackend:
    """Instantiate a backend by display name. Raises KeyError if unknown."""
    return BACKEND_REGISTRY[name]()


def available_backends() -> list[str]:
    """Return display names of all registered backends."""
    return list(BACKEND_REGISTRY.keys())


BACKEND_MODULES = ("mprime", "stress_ng", "stressapptest", "ycruncher")


def load_all() -> None:
    """Import all backend modules to trigger registration."""
    for name in BACKEND_MODULES:
        importlib.import_module(f"{__name__}.{name}")
