"""Stress test backend registry — auto-discovers available backends."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from engine.backends.base import StressBackend

# Registry: display_name -> backend class
# Populated by register_backend() calls in each backend module.
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


def load_all() -> None:
    """Import all backend modules to trigger registration."""
    import engine.backends.mprime  # noqa: F401
    import engine.backends.stress_ng  # noqa: F401
    import engine.backends.ycruncher  # noqa: F401
    import engine.backends.stressapptest  # noqa: F401
