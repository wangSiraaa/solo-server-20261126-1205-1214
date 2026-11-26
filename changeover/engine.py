"""Convenience wiring: an ``Engine`` on top of a durable ``Store``."""

from .service import Engine
from .store import Store


def build_engine(path):
    store = Store(path)
    return Engine(store)


__all__ = ["Engine", "build_engine"]
