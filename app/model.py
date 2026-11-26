"""Pure domain model: bias math, qualification, decision rules.

Conventions
-----------
* All lengths and biases are **integers** (gradations).
* A reading's bias is ``start_bias + extension_bias`` when the measured span
  crosses the extension joint, else just ``start_bias``.
* ``true = reading - bias`` for a single (direct) reading.
* For two readings on the same ruler subtracted to get a board length, each
  reading is corrected independently, then subtracted:
  ``true_len = (b - bias_b) - (a - bias_a)``.  When the same configuration is
  used for both readings the bias cancels exactly -- a subtract measurement is
  *insensitive* to a piece bias, a direct one is not.
* Qualification is judged against the **physical true length**, never against
  the raw reading and never against an assumed/nominal bias.

Chosen recheck route
--------------------
**随件复核 (accompanying recheck)** -- every board keeps one still-reachable
reference point at order acceptance.  After a calibration we recompute every
board from its *real* measuring method; only boards whose true length no longer
fits the assembly clearance get a board work order.  A board whose reference
point has already been covered cannot be rechecked on site and must be
returned to the workshop (recall).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional

KIND_RELEASE = "release"   # 放行
KIND_RECHECK = "recheck"   # 复核 (on-site, needs a reachable reference point)
KIND_REPLACE = "replace"   # 换件 (swap a discrepant insert; piece-scoped)
KIND_RECALL = "recall"     # 召回 (return to workshop; reference point covered)

BOARD_KINDS = (KIND_RELEASE, KIND_RECHECK, KIND_RECALL)


def _get(d, key, default=None):
    if d is None:
        return default
    return d.get(key, default)


def reading_bias(config: Optional[dict], uses_extension: bool,
                 bias_of: Dict[str, int]) -> int:
    """Total offset carried by one reading of ``config``.

    ``bias_of`` maps piece_id -> true bias.  Missing pieces contribute 0.
    """
    start = _get(config, "start_piece")
    b = int(bias_of.get(start, 0)) if start else 0
    if uses_extension:
        ext = _get(config, "extension_piece")
        if ext:
            b += int(bias_of.get(ext, 0))
    return b


def _read_value(reading) -> int:
    """Accept either a bare int or a ``{"value": int}`` mapping."""
    if isinstance(reading, dict):
        return int(reading["value"])
    return int(reading)


def _read_uses_ext(reading, default: bool) -> bool:
    if isinstance(reading, dict) and "uses_extension" in reading:
        return bool(reading["uses_extension"])
    return default


@dataclass
class Measurement:
    """A single measurement record: raw evidence, not yet corrected."""

    board_id: str
    ruler_id: str
    method: str                       # "direct" | "subtract"
    config: dict = field(default_factory=dict)
    uses_extension: bool = False
    reading: Optional[int] = None     # direct (int or mapping via helpers)
    reading_a: Optional[int] = None   # subtract: smaller reading
    reading_b: Optional[int] = None   # subtract: larger reading
    config_a: Optional[dict] = None   # defaults to config
    config_b: Optional[dict] = None   # defaults to config
    a_uses_extension: Optional[bool] = None
    b_uses_extension: Optional[bool] = None
    id: Optional[str] = None

    def _bias_a(self, bias_of):
        return reading_bias(self.config_a or self.config,
                            self._ext_a(), bias_of)

    def _bias_b(self, bias_of):
        return reading_bias(self.config_b or self.config,
                            self._ext_b(), bias_of)

    def _ext_a(self):
        return self.uses_extension if self.a_uses_extension is None else self.a_uses_extension

    def _ext_b(self):
        return self.uses_extension if self.b_uses_extension is None else self.b_uses_extension

    def true_length(self, bias_of: Dict[str, int]) -> int:
        if self.method == "direct":
            if self.reading is None:
                raise ValueError("direct measurement needs 'reading'")
            val = _read_value(self.reading)
            ue = _read_uses_ext(self.reading, self.uses_extension)
            return val - reading_bias(self.config, ue, bias_of)
        if self.method == "subtract":
            if self.reading_a is None or self.reading_b is None:
                raise ValueError("subtract measurement needs reading_a/reading_b")
            a = _read_value(self.reading_a)
            b = _read_value(self.reading_b)
            return (b - self._bias_b(bias_of)) - (a - self._bias_a(bias_of))
        raise ValueError(f"unknown method {self.method!r}")

    def bias_sensitive(self, actual_of: Dict[str, int],
                       nominal_of: Dict[str, int]) -> bool:
        """Whether a piece discrepancy changes the inferred true length."""
        return self.true_length(actual_of) != self.true_length(nominal_of)


def qualifies(true_len: int, min_len: Optional[int],
              max_len: Optional[int]) -> bool:
    if min_len is not None and true_len < min_len:
        return False
    if max_len is not None and true_len > max_len:
        return False
    return True


def clearance_margin(true_len: int, min_len: Optional[int],
                     max_len: Optional[int]) -> Optional[int]:
    """Smallest signed slack to either bound (None when unbounded both ways)."""
    margins = []
    if min_len is not None:
        margins.append(true_len - min_len)
    if max_len is not None:
        margins.append(max_len - true_len)
    return min(margins) if margins else None


def measurement_chain(measurement: Measurement, requirement: dict,
                      actual_of: Dict[str, int], nominal_of: Dict[str, int],
                      ref_reachable: bool) -> dict:
    """Assemble the auditable measurement chain for a disposition."""
    used = []
    for cfg in (measurement.config, measurement.config_a, measurement.config_b):
        for pid in ((cfg or {}).get("start_piece"), (cfg or {}).get("extension_piece")):
            if pid and pid not in used:
                used.append(pid)
    true_len = measurement.true_length(actual_of)
    nom_len = measurement.true_length(nominal_of)
    min_len = requirement.get("min")
    max_len = requirement.get("max")
    return {
        "board_id": measurement.board_id,
        "ruler_id": measurement.ruler_id,
        "method": measurement.method,
        "config": measurement.config,
        "uses_extension": measurement.uses_extension,
        "readings": {
            "reading": measurement.reading,
            "reading_a": measurement.reading_a,
            "reading_b": measurement.reading_b,
        },
        "piece_biases": {pid: int(actual_of.get(pid, 0)) for pid in used},
        "nominal_biases": {pid: int(nominal_of.get(pid, 0)) for pid in used},
        "true_length": true_len,
        "nominal_true_length": nom_len,
        "requirement": {"min": min_len, "max": max_len},
        "margin": clearance_margin(true_len, min_len, max_len),
        "qualified": qualifies(true_len, min_len, max_len),
        "bias_sensitive": true_len != nom_len,
        "ref_reachable": ref_reachable,
    }


@dataclass
class BoardJudgement:
    board_id: str
    true_length: int
    qualified: bool
    margin: Optional[int]
    bias_sensitive: bool
    ref_reachable: bool
    action: str
    reason: str


def discrepant_pieces(actual_of: Dict[str, int],
                      nominal_of: Dict[str, int]) -> List[str]:
    """Pieces whose actual bias differs from nominal (the 'off by two' set)."""
    out = []
    for pid, act in actual_of.items():
        if int(act) != int(nominal_of.get(pid, 0)):
            out.append(pid)
    return sorted(out)


def decide_board_action(*, true_len: int, min_len: Optional[int],
                        max_len: Optional[int], ref_reachable: bool) -> tuple:
    """Pick (action, reason) for a board under the accompanying-recheck route."""
    if qualifies(true_len, min_len, max_len):
        return KIND_RELEASE, "true length fits assembly clearance"
    if ref_reachable:
        return KIND_RECHECK, ("true length fails; reference point reachable "
                              "for on-site recheck")
    return KIND_RECALL, ("true length fails; reference point covered, "
                         "must return to workshop")
