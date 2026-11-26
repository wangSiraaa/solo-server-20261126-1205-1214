"""Domain values shared by the engine and the service layer."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class PieceView:
    piece_id: str
    title: str
    points_required: int
    duration: int
    wish_points: int
    eligible: tuple[str, ...]


@dataclass(frozen=True)
class PledgeView:
    pledge_id: str
    supporter: str
    points: int
    remaining: int          # points still chargeable for this pledge
    accepts: tuple[str, ...]
    requires: tuple[str, ...]
    active: bool


@dataclass(frozen=True)
class World:
    """A read-only snapshot used to decide feasibility.

    ``free_slots`` maps translator id -> list of ``(ord, label)`` shifts that
    are *globally* unbooked at this moment.
    """

    issue_id: str
    mode: str
    pieces: dict[str, PieceView]
    opened: frozenset[str]
    free_slots: dict[str, list[tuple[int, str]]]
    pledges: tuple[PledgeView, ...]
    supporter_remaining: dict[str, int]   # remaining per-issue cap
    supporter_caps: dict[str, int]

    def piece(self, piece_id: str) -> PieceView:
        return self.pieces[piece_id]


@dataclass
class SlotAssignment:
    piece_id: str
    translator_id: str
    slots: list[str]


@dataclass
class FundingLine:
    supporter: str
    pledge_id: str
    points: int


@dataclass
class Plan:
    """A fully realised opening: every piece has a translator and every
    deducted point is traced to one pledge."""

    pieces: list[str]
    assignments: dict[str, SlotAssignment]
    funding: dict[str, list[FundingLine]]
    supporter_deducted: dict[str, int]
    pledge_deducted: dict[str, int]

    def candidate_id(self) -> str:
        return "|".join(sorted(self.pieces))

    def to_public(self) -> dict:
        return {
            "candidate_id": self.candidate_id(),
            "pieces": [
                {
                    "piece_id": pid,
                    "translator_id": self.assignments[pid].translator_id,
                    "slots": list(self.assignments[pid].slots),
                    "points": sum(f.points for f in self.funding[pid]),
                    "funding": [
                        {
                            "supporter": f.supporter,
                            "pledge_id": f.pledge_id,
                            "points": f.points,
                        }
                        for f in self.funding[pid]
                    ],
                }
                for pid in self.pieces
            ],
            "supporter_deducted": dict(self.supporter_deducted),
        }
