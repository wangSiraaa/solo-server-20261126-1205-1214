"""Beadworks: a small counter service for beaded chains and consigned pieces."""

from .service import Beadworks, BeadworksError, NotFoundError, ConflictError, RemovalNotFeasible

__all__ = [
    "Beadworks",
    "BeadworksError",
    "NotFoundError",
    "ConflictError",
    "RemovalNotFeasible",
]

__version__ = "1.0.0"
