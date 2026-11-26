"""Service engine: rental state, calibration backfill, work-order dispatch,
claims (idempotent), recheck/workshop completions, and tenant-facing views.

Everything is integer-valued.  The state object is a plain dict so it can be
snapshotted to SQLite and reloaded after a restart; the engine never keeps
correctness-critical data only in memory.
"""

from __future__ import annotations

import copy
import uuid
from typing import Dict, List, Optional

from . import model
from .store import Store


class ServiceError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.message = message
        self.status = status


# ---------------------------------------------------------------------------
# value helpers
# ---------------------------------------------------------------------------

def _req_int(value, name: str) -> int:
    if isinstance(value, bool):
        raise ServiceError(f"{name} must be an integer")
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    raise ServiceError(f"{name} must be an integer (integer lengths only)")


def _opt_int(value, name: str) -> Optional[int]:
    if value is None:
        return None
    return _req_int(value, name)


def _req_str(value, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ServiceError(f"{name} must be a non-empty string")
    return value


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


_EMPTY_STATE = {
    "pieces": {},
    "rulers": {},
    "boxes": {},
    "boards": {},
    "measurements": {},
    "piece_receipts": [],
    "calibrations": {},
    "work_orders": {},
    "wo_index": {},           # dedupe key -> wo_id
    "dispositions": {},
    "claim_index": {},        # screen token -> disposition id
    "valid_disp": {},         # work_order_id -> valid disposition id
    "recheck_slots": {},      # slot_id -> slot record
    "seq": 0,
}


class Service:
    def __init__(self, store: Store, *, slot_capacity: int = 3):
        self.store = store
        self.slot_capacity = max(1, int(slot_capacity))
        self.state = store.load_state() or copy.deepcopy(_EMPTY_STATE)
        for k, v in _EMPTY_STATE.items():
            self.state.setdefault(k, copy.deepcopy(v))

    # -- persistence --------------------------------------------------------
    def _commit(self) -> None:
        self.store.save_state(self.state)

    def _next_seq(self) -> int:
        self.state["seq"] = int(self.state.get("seq", 0)) + 1
        return self.state["seq"]

    def _event(self, kind: str, **body) -> int:
        seq = self._next_seq()
        try:
            self.store.append_event(seq, dict(kind=kind, seq=seq, **body))
        except Exception:
            pass
        return seq

    # -- bias maps ----------------------------------------------------------
    def _true_bias_map(self) -> Dict[str, int]:
        return {pid: int(p.get("true_bias", 0))
                for pid, p in self.state["pieces"].items()}

    def _nominal_bias_map(self) -> Dict[str, int]:
        return {pid: int(p.get("nominal_bias", 0))
                for pid, p in self.state["pieces"].items()}

    # =====================================================================
    # setup: pieces / rulers / boxes / boards
    # =====================================================================
    def upsert_piece(self, *, piece_id: str, kind: str,
                     nominal_bias: int = 0,
                     true_bias: Optional[int] = None) -> dict:
        piece_id = _req_str(piece_id, "piece_id")
        if kind not in ("start", "extension"):
            raise ServiceError("kind must be 'start' or 'extension'")
        nom = _req_int(nominal_bias, "nominal_bias")
        piece = self.state["pieces"].get(piece_id, {})
        piece.update({
            "id": piece_id,
            "kind": kind,
            "nominal_bias": nom,
            "true_bias": nom if true_bias is None else _req_int(true_bias, "true_bias"),
        })
        self.state["pieces"][piece_id] = piece
        self._commit()
        return piece

    def create_ruler(self, *, ruler_id: str) -> dict:
        ruler_id = _req_str(ruler_id, "ruler_id")
        ruler = self.state["rulers"].setdefault(ruler_id, {"id": ruler_id})
        self._commit()
        return ruler

    def create_box(self, *, box_id: str, piece_ids: List[str],
                   tenant_id: Optional[str] = None) -> dict:
        box_id = _req_str(box_id, "box_id")
        for pid in piece_ids:
            if pid not in self.state["pieces"]:
                raise ServiceError(f"unknown piece {pid}", 404)
        box = {
            "id": box_id,
            "piece_ids": list(piece_ids),
            "tenant_id": tenant_id,
            # NOTE: holding a box does NOT imply every order used every piece.
        }
        self.state["boxes"][box_id] = box
        self._commit()
        return box

    def create_board(self, *, board_id: str, tenant_id: str,
                     min_len: Optional[int] = None,
                     max_len: Optional[int] = None,
                     ref_point_id: str,
                     ref_reachable: bool = True,
                     box_id: Optional[str] = None,
                     lease_id: Optional[str] = None) -> dict:
        board_id = _req_str(board_id, "board_id")
        tenant_id = _req_str(tenant_id, "tenant_id")
        mn = _opt_int(min_len, "min_len")
        mx = _opt_int(max_len, "max_len")
        if mn is not None and mx is not None and mn > mx:
            raise ServiceError("min_len cannot exceed max_len")
        board = {
            "id": board_id,
            "tenant_id": tenant_id,
            "box_id": box_id,
            "lease_id": lease_id,
            "requirement": {"min": mn, "max": mx},
            "ref_point": {"id": _req_str(ref_point_id, "ref_point_id"),
                          "reachable": bool(ref_reachable)},
            "order_measurement_id": None,
            "current_measurement_id": None,
        }
        self.state["boards"][board_id] = board
        self._commit()
        return board

    def set_ref_reachable(self, *, board_id: str, reachable: bool) -> dict:
        board = self._board(board_id)
        board["ref_point"]["reachable"] = bool(reachable)
        self._commit()
        return board

    def set_requirement(self, *, board_id: str,
                        min_len: Optional[int] = None,
                        max_len: Optional[int] = None) -> dict:
        board = self._board(board_id)
        mn = _opt_int(min_len, "min_len")
        mx = _opt_int(max_len, "max_len")
        if mn is not None and mx is not None and mn > mx:
            raise ServiceError("min_len cannot exceed max_len")
        board["requirement"] = {"min": mn, "max": mx}
        self._commit()
        return board

    # =====================================================================
    # measurements / usage receipts
    # =====================================================================
    def _board(self, board_id: str) -> dict:
        board = self.state["boards"].get(board_id)
        if not board:
            raise ServiceError(f"unknown board {board_id}", 404)
        return board

    def _make_measurement(self, payload: dict, *, board_id: str,
                          source: str) -> dict:
        method = payload.get("method")
        if method not in ("direct", "subtract"):
            raise ServiceError("method must be 'direct' or 'subtract'")
        ruler_id = _req_str(payload.get("ruler_id"), "ruler_id")
        config = payload.get("config") or {}
        uses_ext = bool(payload.get("uses_extension", False))

        m = {
            "id": _new_id("m"),
            "board_id": board_id,
            "ruler_id": ruler_id,
            "source": source,
            "method": method,
            "config": self._validate_config(config),
            "uses_extension": uses_ext,
            "seq": self._next_seq(),
        }
        if method == "direct":
            m["reading"] = _req_int(payload.get("reading"), "reading")
        else:
            m["reading_a"] = _req_int(payload.get("reading_a"), "reading_a")
            m["reading_b"] = _req_int(payload.get("reading_b"), "reading_b")
            if payload.get("config_a") is not None:
                m["config_a"] = self._validate_config(payload["config_a"])
            if payload.get("config_b") is not None:
                m["config_b"] = self._validate_config(payload["config_b"])
        self.state["measurements"][m["id"]] = m
        return m

    def _validate_config(self, config: dict) -> dict:
        if not isinstance(config, dict):
            raise ServiceError("config must be an object")
        out = {}
        sp = config.get("start_piece")
        ep = config.get("extension_piece")
        if sp is not None:
            sp = _req_str(sp, "start_piece")
            if sp not in self.state["pieces"]:
                raise ServiceError(f"unknown start_piece {sp}", 404)
            out["start_piece"] = sp
        if ep is not None:
            ep = _req_str(ep, "extension_piece")
            if ep not in self.state["pieces"]:
                raise ServiceError(f"unknown extension_piece {ep}", 404)
            out["extension_piece"] = ep
        return out

    def submit_usage_receipt(self, payload: dict) -> dict:
        """Record the order-time usage receipt (the on-site measurement).

        This is the board's *first* usage record: ruler + installed pieces +
        how the length was read.  It is used later by calibration backfill.
        """
        board_id = _req_str(payload.get("board_id"), "board_id")
        board = self._board(board_id)
        m = self._make_measurement(payload, board_id=board_id,
                                   source="order_usage")
        board["order_measurement_id"] = m["id"]
        board["current_measurement_id"] = m["id"]
        receipt = {
            "id": _new_id("rcpt"),
            "board_id": board_id,
            "measurement_id": m["id"],
            "ruler_id": m["ruler_id"],
            "config": m["config"],
            "method": m["method"],
            "seq": m["seq"],
            "source": payload.get("source", "order"),
        }
        self.state["piece_receipts"].append(receipt)
        self._commit()
        return {"receipt": receipt, "measurement": m}

    def replace_usage_receipt(self, *, board_id: str, payload: dict,
                             swap_reason: str = "on-site piece swap") -> dict:
        """Replace the order-time usage receipt with a new configuration.

        Models an on-site swap receipt (现场换片回执): the ruler now carries a
        different piece.  A fresh order_usage measurement replaces the old one.
        """
        board = self._board(board_id)
        m = self._make_measurement(payload, board_id=board_id,
                                   source="order_usage")
        old_id = board.get("order_measurement_id")
        old = self.state["measurements"].get(old_id) if old_id else None
        board["order_measurement_id"] = m["id"]
        board["current_measurement_id"] = m["id"]
        receipt = {
            "id": _new_id("rcpt"),
            "board_id": board_id,
            "measurement_id": m["id"],
            "replaces_measurement_id": old_id,
            "ruler_id": m["ruler_id"],
            "config": m["config"],
            "method": m["method"],
            "seq": m["seq"],
            "source": "swap",
            "reason": swap_reason,
        }
        self.state["piece_receipts"].append(receipt)
        self._commit()
        return {"receipt": receipt, "measurement": m,
                "replaced": old}

    def record_piece_event(self, *, event_type: str, piece_id: str,
                           ruler_id: Optional[str] = None,
                           box_id: Optional[str] = None,
                           board_id: Optional[str] = None) -> dict:
        """Record a return / seal / open / swap style provenance event.

        These establish which ruler a piece was on in a rental period; they do
        not by themselves attribute the piece to an order's measurement.
        """
        if event_type not in ("return", "seal", "open", "swap"):
            raise ServiceError("event_type must be one of return|seal|open|swap")
        if piece_id not in self.state["pieces"]:
            raise ServiceError(f"unknown piece {piece_id}", 404)
        ev = {
            "id": _new_id("pev"),
            "type": event_type,
            "piece_id": piece_id,
            "ruler_id": ruler_id,
            "box_id": box_id,
            "board_id": board_id,
            "seq": self._next_seq(),
        }
        self.state["piece_receipts"].append(ev)
        self._commit()
        return ev

    # =====================================================================
    # calibration + evaluation + dispatch
    # =====================================================================
    def add_calibration(self, *, piece_biases: Dict[str, int],
                        note: Optional[str] = None,
                        calibration_id: Optional[str] = None) -> dict:
        """Backfill a calibration: reveal the true bias of one or more pieces."""
        if not piece_biases:
            raise ServiceError("piece_biases must not be empty")
        calib_id = calibration_id or _new_id("cal")
        entries = {}
        for pid, bias in piece_biases.items():
            if pid not in self.state["pieces"]:
                raise ServiceError(f"unknown piece {pid}", 404)
            b = _req_int(bias, f"bias[{pid}]")
            entries[pid] = b
            self.state["pieces"][pid]["true_bias"] = b
        calib = {
            "id": calib_id,
            "pieces": entries,
            "note": note,
            "seq": self._next_seq(),
        }
        self.state["calibrations"][calib_id] = calib
        self._commit()
        return calib

    # -- measurement evaluation --------------------------------------------
    def _measurement_obj(self, m: dict) -> model.Measurement:
        return model.Measurement(
            board_id=m["board_id"], ruler_id=m["ruler_id"],
            method=m["method"], config=m.get("config") or {},
            uses_extension=bool(m.get("uses_extension", False)),
            reading=m.get("reading"),
            reading_a=m.get("reading_a"), reading_b=m.get("reading_b"),
            config_a=m.get("config_a"), config_b=m.get("config_b"),
            id=m["id"],
        )

    def evaluate_board(self, board_id: str) -> dict:
        board = self._board(board_id)
        mid = board.get("current_measurement_id")
        if not mid:
            raise ServiceError(f"board {board_id} has no measurement yet")
        m = self.state["measurements"][mid]
        obj = self._measurement_obj(m)
        true_map = self._true_bias_map()
        nominal_map = self._nominal_bias_map()
        req = board["requirement"]
        ref_reachable = bool(board["ref_point"]["reachable"])
        true_len = obj.true_length(true_map)
        nom_len = obj.true_length(nominal_map)
        action, reason = model.decide_board_action(
            true_len=true_len, min_len=req.get("min"), max_len=req.get("max"),
            ref_reachable=ref_reachable)
        chain = model.measurement_chain(
            obj, req, true_map, nominal_map, ref_reachable)
        chain["measurement_id"] = mid
        chain["source"] = m.get("source")
        chain["seq"] = m.get("seq")
        chain["ref_point"] = dict(board["ref_point"])
        return {
            "board_id": board_id,
            "tenant_id": board["tenant_id"],
            "box_id": board.get("box_id"),
            "true_length": true_len,
            "nominal_true_length": nom_len,
            "qualified": model.qualifies(true_len, req.get("min"), req.get("max")),
            "margin": model.clearance_margin(true_len, req.get("min"), req.get("max")),
            "bias_sensitive": true_len != nom_len,
            "ref_reachable": ref_reachable,
            "action": action,
            "reason": reason,
            "chain": chain,
        }

    def _scope_boards(self, box_id: Optional[str],
                      tenant_id: Optional[str],
                      board_ids: Optional[List[str]]) -> List[str]:
        if board_ids:
            return list(board_ids)
        out = []
        for bid, b in self.state["boards"].items():
            if box_id is not None and b.get("box_id") != box_id:
                continue
            if tenant_id is not None and b.get("tenant_id") != tenant_id:
                continue
            if not b.get("current_measurement_id"):
                continue
            out.append(bid)
        return sorted(out)

    def dispatch_calibration(self, *, calibration_id: str,
                             box_id: Optional[str] = None,
                             tenant_id: Optional[str] = None,
                             board_ids: Optional[List[str]] = None,
                             slot_capacity: Optional[int] = None) -> dict:
        """Evaluate the calibration scope and dispatch resident work orders.

        Board work orders are upserted per (calibration, board) so a re-dispatch
        after an edited receipt/requirement updates the still-open order instead
        of duplicating it.  Unaffected or still-qualifying boards get *release*
        orders -- never a box-wide recall.
        """
        if calibration_id not in self.state["calibrations"]:
            raise ServiceError(f"unknown calibration {calibration_id}", 404)
        cap = max(1, int(slot_capacity or self.slot_capacity))
        bids = self._scope_boards(box_id, tenant_id, board_ids)
        evaluations = [self.evaluate_board(b) for b in bids]

        # boards that need on-site rework (recheck/recall) drive piece replace,
        # but only when the discrepancy actually moves the board's true length
        # (a subtract measurement cancels the bias, so it must not blame a piece).
        rework_piece_users: Dict[str, List[str]] = {}
        wo_ids: List[str] = []
        for ev in evaluations:
            wo = self._upsert_board_wo(calibration_id, ev)
            wo_ids.append(wo["id"])
            if ev["action"] in (model.KIND_RECHECK, model.KIND_RECALL) \
                    and ev["bias_sensitive"]:
                for pid in ev["chain"]["piece_biases"]:
                    rework_piece_users.setdefault(pid, []).append(ev["board_id"])

        # piece-level replace orders: only for discrepant pieces that were
        # actually used by a board needing rework (and that the board's
        # measurement was sensitive to).  Merely sitting in the box is not
        # enough -- and a board that used a *different*, good piece must not
        # be dragged in.
        discrepant = set(model.discrepant_pieces(self._true_bias_map(),
                                                 self._nominal_bias_map()))
        replace_ids = []
        for pid in sorted(rework_piece_users):
            if pid in discrepant:
                wo = self._upsert_piece_wo(calibration_id, pid,
                                           rework_piece_users[pid])
                replace_ids.append(wo["id"])

        # assign on-site recheck boards to integer recheck slots.
        self._assign_recheck_slots(calibration_id, evaluations, cap)
        self._commit()
        return {
            "calibration_id": calibration_id,
            "evaluations": evaluations,
            "work_order_ids": wo_ids,
            "replace_order_ids": replace_ids,
        }

    def _find_open_wo(self, key: str) -> Optional[dict]:
        wid = self.state["wo_index"].get(key)
        if not wid:
            return None
        wo = self.state["work_orders"].get(wid)
        if not wo:
            return None
        return wo

    def _upsert_board_wo(self, calib_id: str, ev: dict) -> dict:
        key = f"board:{calib_id}:{ev['board_id']}"
        existing = self._find_open_wo(key)
        desired = ev["action"]
        if existing:
            if existing["status"] == "completed" and desired == model.KIND_RELEASE:
                return existing
            if existing["status"] == "completed" and desired != model.KIND_RELEASE:
                # a later edit made a previously released board fail again:
                # reopen and retract the stale delivery.
                self._retract_valid_disposition(existing["id"])
                existing["status"] = "open"
            elif existing["kind"] != desired:
                # the correct action for this board changed (e.g. recheck ->
                # recall); any live claim on the old action is now invalid.
                self._retract_valid_disposition(existing["id"])
                if existing["status"] == "claimed":
                    existing["status"] = "open"
            existing["kind"] = ev["action"]
            existing["reason"] = ev["reason"]
            existing["chain"] = ev["chain"]
            existing["evaluation"] = {
                "true_length": ev["true_length"],
                "qualified": ev["qualified"],
                "margin": ev["margin"],
                "bias_sensitive": ev["bias_sensitive"],
            }
            existing["requirement"] = dict(
                self.state["boards"][ev["board_id"]]["requirement"])
            return existing
        wo = {
            "id": _new_id("wo"),
            "calibration_id": calib_id,
            "scope": "board",
            "board_id": ev["board_id"],
            "tenant_id": ev["tenant_id"],
            "box_id": ev.get("box_id"),
            "kind": ev["action"],
            "reason": ev["reason"],
            "status": "open",
            "requirement": dict(self.state["boards"][ev["board_id"]]["requirement"]),
            "evaluation": {
                "true_length": ev["true_length"],
                "qualified": ev["qualified"],
                "margin": ev["margin"],
                "bias_sensitive": ev["bias_sensitive"],
            },
            "chain": ev["chain"],
            "slot": None,
            "created_seq": self._next_seq(),
        }
        self.state["work_orders"][wo["id"]] = wo
        self.state["wo_index"][key] = wo["id"]
        return wo

    def _retract_valid_disposition(self, work_order_id: str) -> None:
        vid = self.state["valid_disp"].pop(work_order_id, None)
        if not vid:
            return
        disp = self.state["dispositions"].get(vid)
        if disp:
            disp["valid"] = False
            disp["status"] = "retracted"
        for tok, did in list(self.state["claim_index"].items()):
            if did == vid:
                del self.state["claim_index"][tok]

    def _upsert_piece_wo(self, calib_id: str, piece_id: str,
                         board_ids: List[str]) -> dict:
        key = f"piece:{calib_id}:{piece_id}"
        existing = self._find_open_wo(key)
        if existing and existing["status"] in ("open", "claimed"):
            existing["affected_boards"] = sorted(set(board_ids))
            return existing
        if existing and existing["status"] == "completed":
            return existing
        piece = self.state["pieces"][piece_id]
        wo = {
            "id": _new_id("wo"),
            "calibration_id": calib_id,
            "scope": "piece",
            "board_id": None,
            "piece_id": piece_id,
            "tenant_id": self.state["boards"][board_ids[0]]["tenant_id"],
            "kind": model.KIND_REPLACE,
            "reason": (f"piece {piece_id} bias {piece.get('true_bias')} "
                       f"differs from nominal {piece.get('nominal_bias')}"),
            "status": "open",
            "affected_boards": sorted(set(board_ids)),
            "nominal_bias": int(piece.get("nominal_bias", 0)),
            "true_bias": int(piece.get("true_bias", 0)),
            "created_seq": self._next_seq(),
        }
        self.state["work_orders"][wo["id"]] = wo
        self.state["wo_index"][key] = wo["id"]
        return wo

    def _assign_recheck_slots(self, calib_id: str, evaluations: List[dict],
                              cap: int) -> None:
        # rebuild this calibration's slot plan from scratch so boards that
        # dropped out of recheck (became release/recall) do not linger.
        for sid in [s for s in self.state["recheck_slots"]
                    if self.state["recheck_slots"][s].get("calibration_id") == calib_id]:
            del self.state["recheck_slots"][sid]
        for wo in self.state["work_orders"].values():
            if wo.get("calibration_id") == calib_id and wo["scope"] == "board":
                wo["slot"] = None

        recheck_boards = sorted(e["board_id"] for e in evaluations
                                if e["action"] == model.KIND_RECHECK)
        for i in range(0, len(recheck_boards), cap):
            chunk = recheck_boards[i: i + cap]
            slot_no = i // cap + 1
            sid = f"slot:{calib_id}:{slot_no}"
            self.state["recheck_slots"][sid] = {
                "id": sid,
                "calibration_id": calib_id,
                "slot": slot_no,
                "board_ids": list(chunk),
                "capacity": cap,
            }
            for bid in chunk:
                wo = self._wo_for_board(calib_id, bid)
                if wo:
                    wo["slot"] = slot_no

    def _wo_for_board(self, calib_id: str, board_id: str) -> Optional[dict]:
        wid = self.state["wo_index"].get(f"board:{calib_id}:{board_id}")
        return self.state["work_orders"].get(wid) if wid else None

    # =====================================================================
    # tenant-facing plan
    # =====================================================================
    def plan(self, *, calibration_id: str) -> dict:
        if calibration_id not in self.state["calibrations"]:
            raise ServiceError(f"unknown calibration {calibration_id}", 404)
        wos = [w for w in self.state["work_orders"].values()
               if w.get("calibration_id") == calibration_id]
        by_kind: Dict[str, List[str]] = {}
        for w in wos:
            by_kind.setdefault(w["kind"], []).append(w["id"])
        slots = sorted(
            [s for s in self.state["recheck_slots"].values()
             if s.get("calibration_id") == calibration_id],
            key=lambda s: s["slot"])
        # reflect the *current* recheck set: boards that were rechecked and
        # released (or became recall) drop out of the reserved slots.
        current_recheck = set(
            w["board_id"] for w in wos
            if w["scope"] == "board" and w["kind"] == model.KIND_RECHECK
            and w["status"] != "completed")
        slot_view = []
        for s in slots:
            ids = [b for b in s["board_ids"] if b in current_recheck]
            if ids:
                slot_view.append({"slot": s["slot"], "capacity": s["capacity"],
                                  "board_ids": ids})
        evidence = {}
        for w in wos:
            if w["scope"] == "board":
                evidence[w["board_id"]] = {
                    "work_order_id": w["id"],
                    "kind": w["kind"],
                    "chain": w.get("chain"),
                }
        not_on_site_releasable = []
        for w in wos:
            if w["scope"] == "board" and w["kind"] == model.KIND_RECALL \
                    and w["status"] != "completed":
                not_on_site_releasable.append(w["board_id"])
        released = sorted(set(
            w["board_id"] for w in wos
            if w["scope"] == "board" and w["kind"] == model.KIND_RELEASE
            and w["status"] != "cancelled"))
        recheck = sorted(set(
            w["board_id"] for w in wos
            if w["scope"] == "board" and w["kind"] == model.KIND_RECHECK))
        recall = sorted(set(not_on_site_releasable))
        return {
            "calibration_id": calibration_id,
            "retest": {
                # the boards already installed at the tenant that must be
                # remeasured before they can be trusted again
                "must_retest": sorted(set(recheck) | set(recall)),
                "on_site_recheck": recheck,
                "workshop_recall": recall,
                "delivered": released,
            },
            "released_boards": released,
            "recheck_boards": recheck,
            "recall_boards": recall,
            "replace_pieces": sorted(set(
                w["piece_id"] for w in wos
                if w["scope"] == "piece" and w["kind"] == model.KIND_REPLACE)),
            "recheck_slots": slot_view,
            "evidence": evidence,
            "work_orders": {w["id"]: self._wo_view(w) for w in wos},
            "counts": {k: len(v) for k, v in by_kind.items()},
        }

    # =====================================================================
    # claims + dispositions (idempotent, single-valid)
    # =====================================================================
    def list_work_orders(self, *, tenant_id: Optional[str] = None,
                         calibration_id: Optional[str] = None,
                         status: Optional[str] = None) -> List[dict]:
        out = []
        for w in self.state["work_orders"].values():
            if tenant_id is not None and w.get("tenant_id") != tenant_id:
                continue
            if calibration_id is not None and w.get("calibration_id") != calibration_id:
                continue
            if status is not None and w.get("status") != status:
                continue
            out.append(self._wo_view(w))
        return sorted(out, key=lambda w: w["created_seq"])

    def _wo_view(self, w: dict) -> dict:
        return {
            "id": w["id"],
            "calibration_id": w.get("calibration_id"),
            "scope": w["scope"],
            "board_id": w.get("board_id"),
            "piece_id": w.get("piece_id"),
            "tenant_id": w.get("tenant_id"),
            "kind": w["kind"],
            "reason": w.get("reason"),
            "status": w["status"],
            "requirement": w.get("requirement"),
            "evaluation": w.get("evaluation"),
            "affected_boards": w.get("affected_boards"),
            "slot": w.get("slot"),
            "created_seq": w["created_seq"],
            "disposition_id": self.state["valid_disp"].get(w["id"]),
        }

    def claim(self, *, work_order_id: str, worker_id: str,
              screen_token: str) -> dict:
        """Claim a work order from a tenant screen.

        Idempotent on ``screen_token`` and keeps at most one *valid* disposition
        per work order.  Two workers on the same old screen therefore resolve to
        a single disposition; this survives a restart because both the claim
        index and the dispositions are persisted.
        """
        wo = self.state["work_orders"].get(work_order_id)
        if not wo:
            raise ServiceError(f"unknown work order {work_order_id}", 404)
        worker_id = _req_str(worker_id, "worker_id")
        screen_token = _req_str(screen_token, "screen_token")

        # 1) same screen token already claimed -> return that disposition
        existing_disp_id = self.state["claim_index"].get(screen_token)
        if existing_disp_id:
            disp = self.state["dispositions"][existing_disp_id]
            if disp["work_order_id"] != work_order_id:
                raise ServiceError(
                    "screen token already used for a different work order", 409)
            return self._disp_view(disp, deduplicated=True)

        # 2) work order already has a valid disposition -> dedupe, do not create
        valid_id = self.state["valid_disp"].get(work_order_id)
        if valid_id:
            disp = self.state["dispositions"][valid_id]
            self.state["claim_index"][screen_token] = valid_id
            self._commit()
            return self._disp_view(disp, deduplicated=True)

        # 3) fresh valid disposition
        disp = self._make_disposition(wo, worker_id, screen_token, valid=True)
        self.state["valid_disp"][work_order_id] = disp["id"]
        self.state["claim_index"][screen_token] = disp["id"]
        if wo["status"] == "open":
            wo["status"] = "claimed"
        self._commit()
        return self._disp_view(disp, deduplicated=False)

    def _make_disposition(self, wo: dict, worker_id: str, screen_token: str,
                          *, valid: bool) -> dict:
        chain = wo.get("chain")
        disp = {
            "id": _new_id("disp"),
            "work_order_id": wo["id"],
            "tenant_id": wo.get("tenant_id"),
            "board_id": wo.get("board_id"),
            "piece_id": wo.get("piece_id"),
            "kind": wo["kind"],
            "slot": wo.get("slot"),
            "worker_id": worker_id,
            "screen_token": screen_token,
            "valid": bool(valid),
            "status": "active",
            "created_seq": self._next_seq(),
            "chain": chain,
        }
        self.state["dispositions"][disp["id"]] = disp
        return disp

    def _disp_view(self, disp: dict, *, deduplicated: bool = False) -> dict:
        out = dict(disp)
        out["deduplicated"] = deduplicated
        return out

    def list_dispositions(self, *, tenant_id: Optional[str] = None,
                          work_order_id: Optional[str] = None,
                          valid_only: bool = True) -> List[dict]:
        out = []
        for d in self.state["dispositions"].values():
            if tenant_id is not None and d.get("tenant_id") != tenant_id:
                continue
            if work_order_id is not None and d.get("work_order_id") != work_order_id:
                continue
            if valid_only and not d.get("valid"):
                continue
            out.append(dict(d))
        return sorted(out, key=lambda d: d["created_seq"])

    def get_measurement_chain(self, *, work_order_id: str) -> dict:
        wo = self.state["work_orders"].get(work_order_id)
        if not wo:
            raise ServiceError(f"unknown work order {work_order_id}", 404)
        valid_id = self.state["valid_disp"].get(work_order_id)
        disp = self.state["dispositions"].get(valid_id) if valid_id else None
        chain = (disp or {}).get("chain") or wo.get("chain")
        return {
            "work_order_id": work_order_id,
            "board_id": wo.get("board_id"),
            "kind": wo["kind"],
            "disposition_id": valid_id,
            "chain": chain,
        }

    # =====================================================================
    # completing recheck / workshop remeasure -> can change the work order
    # =====================================================================
    def complete_recheck(self, *, board_id: str, payload: dict,
                         worker_id: Optional[str] = None) -> dict:
        """On-site recheck: remeasure the board, re-judge, update its order."""
        board = self._board(board_id)
        m = self._make_measurement(payload, board_id=board_id, source="recheck")
        board["current_measurement_id"] = m["id"]
        self._commit()
        return self._reresolve_board(board_id, m, by="recheck",
                                     worker_id=worker_id)

    def complete_workshop_remeasure(self, *, board_id: str, payload: dict,
                                    worker_id: Optional[str] = None) -> dict:
        """Workshop remeasure after a return; the reading can change the order."""
        board = self._board(board_id)
        m = self._make_measurement(payload, board_id=board_id,
                                   source="workshop_remeasure")
        board["current_measurement_id"] = m["id"]
        self._commit()
        return self._reresolve_board(board_id, m, by="workshop",
                                     worker_id=worker_id)

    def _reresolve_board(self, board_id: str, measurement: dict, *,
                         by: str, worker_id: Optional[str]) -> dict:
        ev = self.evaluate_board(board_id)
        board = self._board(board_id)
        # find the board's current work order (latest calibration touching it)
        wos = [w for w in self.state["work_orders"].values()
               if w.get("scope") == "board" and w.get("board_id") == board_id]
        wo = max(wos, key=lambda w: w["created_seq"]) if wos else None
        changed_from = None
        if wo is None:
            # no calibration dispatch yet: synthesize a measurement-only order
            wo = {
                "id": _new_id("wo"),
                "calibration_id": None,
                "scope": "board",
                "board_id": board_id,
                "tenant_id": board["tenant_id"],
                "box_id": board.get("box_id"),
                "kind": ev["action"],
                "reason": ev["reason"],
                "status": "open",
                "requirement": dict(board["requirement"]),
                "evaluation": None,
                "chain": ev["chain"],
                "slot": None,
                "created_seq": self._next_seq(),
            }
            self.state["work_orders"][wo["id"]] = wo
        else:
            changed_from = wo["kind"]
            wo["kind"] = ev["action"]
            wo["reason"] = ev["reason"]
            wo["chain"] = ev["chain"]
            wo["evaluation"] = {
                "true_length": ev["true_length"],
                "qualified": ev["qualified"],
                "margin": ev["margin"],
                "bias_sensitive": ev["bias_sensitive"],
            }
            wo["requirement"] = dict(board["requirement"])

        if ev["action"] == model.KIND_RELEASE:
            wo["status"] = "completed"
            # close the valid disposition as delivered
            vid = self.state["valid_disp"].get(wo["id"])
            if vid:
                self.state["dispositions"][vid]["status"] = "delivered"
                self.state["dispositions"][vid]["chain"] = ev["chain"]
        else:
            if wo.get("status") == "completed":
                # a remeasure made a delivered board fail again: retract and reopen
                self._retract_valid_disposition(wo["id"])
                wo["status"] = "open"
            elif wo.get("status") != "claimed":
                wo["status"] = "open"

        self._commit()
        return {
            "board_id": board_id,
            "by": by,
            "worker_id": worker_id,
            "measurement": measurement,
            "work_order": self._wo_view(wo),
            "changed_from": changed_from,
            "evaluation": ev,
        }

    # =====================================================================
    # query helpers
    # =====================================================================
    def get_board(self, board_id: str) -> dict:
        board = self._board(board_id)
        out = copy.deepcopy(board)
        if board.get("current_measurement_id"):
            m = self.state["measurements"][board["current_measurement_id"]]
            out["measurement"] = m
        return out

    def get_piece(self, piece_id: str) -> dict:
        if piece_id not in self.state["pieces"]:
            raise ServiceError(f"unknown piece {piece_id}", 404)
        return copy.deepcopy(self.state["pieces"][piece_id])

    def snapshot(self) -> dict:
        return copy.deepcopy(self.state)
