"""In-memory/local-file order service with quote, confirmation and claim locks."""

from __future__ import annotations

import copy
import json
import secrets
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .catalog import catalog_hash, default_catalog, revised_clearance_catalog
from .floorplan import normalize_floorplan
from .planner import plan_delivery
from .scenarios import seed_state
from .verifier import replay_step_sheet


class ServiceError(Exception):
    def __init__(self, code: str, message: str, status: int = 400, details: Any = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.details = details or {}


class ShopService:
    def __init__(self, persist_path: Optional[str] = None, seed: bool = True):
        self.lock = threading.RLock()
        self.persist_path = Path(persist_path) if persist_path else None
        self.catalog: Dict[str, Any] = default_catalog()
        self.catalog_history: List[Dict[str, Any]] = []
        self.floorplans: Dict[str, Dict[str, Any]] = {}
        self.measurements: Dict[str, List[Dict[str, Any]]] = {}
        self.quotes: Dict[str, Dict[str, Any]] = {}
        self.orders: Dict[str, Dict[str, Any]] = {}
        self.counters: Dict[str, int] = {}
        if seed:
            self.reset()

    # ---------- basic state ----------

    def reset(self) -> Dict[str, Any]:
        with self.lock:
            data = seed_state()
            self.catalog = data["catalog"]
            self.catalog_history = [copy.deepcopy(self.catalog)]
            self.floorplans = data["floorplans"]
            self.measurements = {}
            self.quotes = {}
            self.orders = {}
            self.counters = {}
            # Canonical two-surveyor doorway history for the narrow scenario.
            self.add_measurement(
                "entry-narrow",
                {
                    "id": "m-door-v1",
                    "surveyor": "量尺员·周",
                    "widthCm": 80,
                    "heightCm": 205,
                    "note": "第一次确认",
                    "confirmedAt": "2026-11-26T09:00:00+08:00",
                },
            )
            self.add_measurement(
                "entry-narrow",
                {
                    "id": "m-door-v2",
                    "surveyor": "量尺员·林",
                    "widthCm": 78,
                    "heightCm": 204,
                    "note": "复核后先后确认的第二版",
                    "confirmedAt": "2026-11-26T10:30:00+08:00",
                },
            )
            self._persist_locked()
            return {"ok": True, "seeded": True}

    def _next_id(self, prefix: str) -> str:
        n = self.counters.get(prefix, 0) + 1
        self.counters[prefix] = n
        return f"{prefix}-{n}"

    def _persist_locked(self) -> None:
        if not self.persist_path:
            return
        payload = {
            "catalog": self.catalog,
            "floorplans": self.floorplans,
            "measurements": self.measurements,
            "quotes": self.quotes,
            "orders": self.orders,
        }
        tmp = self.persist_path.with_suffix(".tmp.json")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.persist_path)

    def health(self) -> Dict[str, Any]:
        return {
            "ok": True,
            "service": "sofa-delivery",
            "catalogHash": catalog_hash(self.catalog),
            "floorPlanCount": len(self.floorplans),
            "quoteCount": len(self.quotes),
            "orderCount": len(self.orders),
            "time": time.time(),
        }

    # ---------- catalog / floor plan / survey ----------

    def get_catalog(self) -> Dict[str, Any]:
        with self.lock:
            return copy.deepcopy(self.catalog)

    def list_floorplans(self) -> List[Dict[str, Any]]:
        with self.lock:
            return [normalize_floorplan(fp).to_dict() for fp in self.floorplans.values()]

    def get_floorplan(self, fp_id: str) -> Dict[str, Any]:
        with self.lock:
            if fp_id not in self.floorplans:
                raise ServiceError("FLOORPLAN_NOT_FOUND", f"no floor plan {fp_id}", 404)
            return normalize_floorplan(self.floorplans[fp_id]).to_dict()

    def upsert_floorplan(self, data: Dict[str, Any]) -> Dict[str, Any]:
        with self.lock:
            fp = normalize_floorplan(data)
            self.floorplans[fp.id] = copy.deepcopy(data)
            self.floorplans[fp.id]["revision"] = int(data.get("revision", 1))
            self._persist_locked()
            return fp.to_dict()

    def set_blocks(
        self, fp_id: str, cells: List[List[int]], block: bool = True, reason: str = ""
    ) -> Dict[str, Any]:
        with self.lock:
            if fp_id not in self.floorplans:
                raise ServiceError("FLOORPLAN_NOT_FOUND", f"no floor plan {fp_id}", 404)
            raw = self.floorplans[fp_id]
            current = {tuple(c) for c in raw.get("propertyBlocked", [])}
            targets = {tuple(c) for c in cells}
            if block:
                current |= targets
            else:
                current -= targets
            raw["propertyBlocked"] = [list(c) for c in sorted(current)]
            raw["revision"] = int(raw.get("revision", 1)) + 1
            raw.setdefault("history", []).append(
                {"revision": raw["revision"], "block": block, "cells": cells, "reason": reason, "at": time.time()}
            )
            self._persist_locked()
            result = normalize_floorplan(raw).to_dict()
            self._invalidate_sheets_for_floorplan_locked(fp_id)
            return result

    def restore_floorplan(self, fp_id: str) -> Dict[str, Any]:
        with self.lock:
            if fp_id not in self.floorplans:
                raise ServiceError("FLOORPLAN_NOT_FOUND", f"no floor plan {fp_id}", 404)
            raw = self.floorplans[fp_id]
            raw["propertyBlocked"] = []
            raw["revision"] = int(raw.get("revision", 1)) + 1
            raw.setdefault("history", []).append(
                {"revision": raw["revision"], "restore": True, "at": time.time()}
            )
            self._persist_locked()
            result = normalize_floorplan(raw).to_dict()
            self._invalidate_sheets_for_floorplan_locked(fp_id)
            return result

    def _invalidate_sheets_for_floorplan_locked(self, fp_id: str) -> None:
        for order in self.orders.values():
            sheet = order.get("stepSheet") or {}
            if sheet.get("floorPlanId") != fp_id:
                continue
            fp = normalize_floorplan(self.floorplans[fp_id])
            validation = replay_step_sheet(
                sheet,
                fp,
                self.catalog,
                order.get("orderRequest"),
                strict_snapshot=False,
            )
            sheet["lastValidation"] = validation
            sheet["status"] = "valid" if validation["valid"] else "stale"
            if not validation["valid"] and order.get("status") == "claimed":
                order["issuanceBlocked"] = True

    def add_measurement(self, door_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        with self.lock:
            versions = self.measurements.setdefault(door_id, [])
            n = len(versions) + 1
            mid = payload.get("id") or f"m-{door_id}-v{n}"
            if any(m["id"] == mid for m in versions):
                raise ServiceError("MEASUREMENT_ID_EXISTS", f"{mid} already exists", 409)
            record = {
                "id": mid,
                "doorId": door_id,
                "version": n,
                "surveyor": payload.get("surveyor", "unknown"),
                "widthCm": float(payload["widthCm"]) if "widthCm" in payload else None,
                "heightCm": float(payload["heightCm"]) if "heightCm" in payload else None,
                "confirmed": bool(payload.get("confirmed", True)),
                "confirmedAt": payload.get("confirmedAt") or time.time(),
                "note": payload.get("note", ""),
                "supersedes": versions[-1]["id"] if versions else None,
                "current": True,
            }
            for m in versions:
                m["current"] = False
            versions.append(record)
            self._persist_locked()
            return copy.deepcopy(record)

    def list_measurements(self, door_id: str) -> List[Dict[str, Any]]:
        with self.lock:
            return copy.deepcopy(self.measurements.get(door_id, []))

    def get_measurement(self, measurement_id: str) -> Dict[str, Any]:
        with self.lock:
            for versions in self.measurements.values():
                for m in versions:
                    if m["id"] == measurement_id:
                        return copy.deepcopy(m)
            raise ServiceError("MEASUREMENT_NOT_FOUND", measurement_id, 404)

    def current_measurement(self, door_id: str) -> Optional[Dict[str, Any]]:
        versions = self.measurements.get(door_id, [])
        return copy.deepcopy(versions[-1]) if versions else None

    def change_clearance(self, model_id: str, clear_cells: List[List[int]], joint_id: str) -> Dict[str, Any]:
        with self.lock:
            self.catalog_history.append(copy.deepcopy(self.catalog))
            self.catalog = revised_clearance_catalog(self.catalog, model_id, clear_cells, joint_id)
            self._persist_locked()
            return copy.deepcopy(self.catalog["models"][model_id]["craft"])

    # ---------- quoting ----------

    def _resolve_fp_and_measurement(self, payload: Dict[str, Any]) -> Tuple[Dict[str, Any], Dict[str, Any], Optional[Dict[str, Any]]]:
        if payload.get("floorPlan"):
            fp_data = payload["floorPlan"]
            fp_id = fp_data.get("id", "inline")
            if fp_id not in self.floorplans and payload.get("persistFloorPlan"):
                self.upsert_floorplan(fp_data)
        else:
            fp_id = payload.get("floorPlanId", "narrow-l-turn")
            if fp_id not in self.floorplans:
                raise ServiceError("FLOORPLAN_NOT_FOUND", f"no floor plan {fp_id}", 404)
            fp_data = self.floorplans[fp_id]

        doorway_id = payload.get("doorwayId") or (payload.get("order") or {}).get("doorwayId")
        measurement_id = payload.get("measurementVersionId") or (payload.get("order") or {}).get("measurementVersionId")
        measurement = None
        if measurement_id:
            measurement = self.get_measurement(measurement_id)
        elif doorway_id:
            measurement = self.current_measurement(doorway_id)
        return fp_data, normalize_floorplan(fp_data).to_dict(), measurement

    @staticmethod
    def _price(plan: Dict[str, Any], catalog: Dict[str, Any], model_id: str) -> Dict[str, Any]:
        prices = catalog["models"][model_id].get("pricing", {})
        base = float(prices.get("baseCny", 399))
        moves = int(plan.get("primitiveMoves", 0))
        step_fee = moves * float(prices.get("stepCny", 2))
        if plan["mode"] == "whole":
            mode_fee = float(prices.get("wholeCny", 260))
            total = base + mode_fee + step_fee
            return {"currency": "CNY", "base": base, "modeFee": mode_fee, "stepFee": step_fee, "total": round(total, 2)}
        trips = int(plan.get("trips", 1))
        first = float(prices.get("piecewiseFirstTripCny", 180))
        extra = float(prices.get("piecewiseAdditionalTripCny", 70)) * max(0, trips - 1)
        total = base + first + extra + step_fee
        return {
            "currency": "CNY",
            "base": base,
            "firstTripFee": first,
            "additionalTripFee": extra,
            "stepFee": step_fee,
            "trips": trips,
            "total": round(total, 2),
        }

    def create_quote(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        with self.lock:
            order_request = dict(payload.get("order") or payload)
            # Remove envelope-only fields if the whole payload was used.
            for k in ("floorPlanId", "floorPlan", "measurementVersionId", "customerId", "idempotencyKey"):
                order_request.pop(k, None)
            fp_data, fp_view, measurement = self._resolve_fp_and_measurement(payload)
            fp_id = fp_view["id"]

            requested_measurement_id = payload.get("measurementVersionId") or order_request.get("measurementVersionId")
            if requested_measurement_id and (not measurement or measurement["id"] != requested_measurement_id):
                measurement = self.get_measurement(requested_measurement_id)

            try:
                plan = plan_delivery(order_request, self.catalog, fp_data)
            except (KeyError, ValueError, TypeError) as exc:
                from .geometry import PlanError

                if isinstance(exc, PlanError) or "unknown component SKU" in str(exc):
                    raise ServiceError("INVALID_ORDER", str(exc), 400) from exc
                raise
            qid = self._next_id("q")
            if not plan.get("feasible"):
                quote = {
                    "quoteId": qid,
                    "status": "infeasible",
                    "feasible": False,
                    "createdAt": time.time(),
                    "customerId": payload.get("customerId"),
                    "floorPlanId": fp_id,
                    "floorPlanHash": fp_view["geometryHash"],
                    "measurementVersionId": measurement["id"] if measurement else None,
                    "measurement": measurement,
                    "plan": plan,
                }
                self.quotes[qid] = quote
                self._persist_locked()
                raise ServiceError(
                    "NO_FEASIBLE_PLAN",
                    "no replayable shop-craft route exists for this order and grid",
                    422,
                    quote,
                )

            price = self._price(plan, self.catalog, plan["modelId"])
            sheet = {
                "sheetId": f"sheet-{qid}",
                "quoteId": qid,
                "version": 1,
                "status": "ready",
                "mode": plan["mode"],
                "steps": plan["steps"],
                "shape": plan.get("shape"),
                "craftLock": plan.get("craftLock"),
                "orientation": plan["orientation"],
                "floorPlanId": fp_id,
                "floorPlanHash": plan["floorPlanHash"],
                "catalogRevision": plan["catalogRevision"],
                "catalogHash": plan["catalogHash"],
                "orderSnapshot": plan["orderSnapshot"],
                "finalPlacements": plan.get("finalPlacements"),
                "assemblyOrder": plan.get("assemblyOrder", []),
                "trips": plan.get("trips"),
                "primitiveMoves": plan.get("primitiveMoves"),
            }
            customer_id = payload.get("customerId") or order_request.get("customerId", "walk-in")
            doorway_id = order_request.get("doorwayId", "default-door")
            quote = {
                "quoteId": qid,
                "status": "quoted",
                "feasible": True,
                "createdAt": time.time(),
                "customerId": customer_id,
                "lockKey": payload.get("lockKey")
                or order_request.get("lockKey")
                or f"customer:{customer_id}:door:{doorway_id}",
                "floorPlanId": fp_id,
                "floorPlanHashAtQuote": plan["floorPlanHash"],
                "catalogRevisionAtQuote": self.catalog.get("revision"),
                "catalogHashAtQuote": catalog_hash(self.catalog),
                "measurementVersionId": measurement["id"] if measurement else None,
                "measurement": measurement,
                "price": price,
                "selectedMode": plan["mode"],
                "alternativeModesFeasible": plan.get("alternativeModesFeasible"),
                "stepSheet": sheet,
                "orderRequest": order_request,
                "confirmation": None,
                "orderId": None,
            }
            self.quotes[qid] = quote
            self._persist_locked()
            return copy.deepcopy(quote)

    def get_quote(self, qid: str) -> Dict[str, Any]:
        with self.lock:
            if qid not in self.quotes:
                raise ServiceError("QUOTE_NOT_FOUND", qid, 404)
            return copy.deepcopy(self.quotes[qid])

    # ---------- confirmation / claim / steps ----------

    def confirm_quote(self, qid: str, payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        payload = payload or {}
        with self.lock:
            quote = self.get_quote(qid)
            idem = payload.get("idempotencyKey")
            lock_key = payload.get("lockKey") or quote.get("lockKey")

            # Idempotent replay of the same confirmation.
            if quote.get("orderId"):
                order = self.orders[quote["orderId"]]
                if idem and quote.get("confirmation", {}).get("idempotencyKey") == idem:
                    return copy.deepcopy(order)
                if order.get("status") in ("claimed", "in_progress", "completed"):
                    # A repeated confirmation of the authoritative quote does
                    # not replace anything; return the existing order.
                    return copy.deepcopy(order)

            # A late/different confirmation must never replace a claimed order.
            for order in self.orders.values():
                if (
                    order.get("lockKey") == lock_key
                    and order.get("status") in ("claimed", "in_progress", "completed")
                    and order.get("quoteId") != qid
                ):
                    raise ServiceError(
                        "ORDER_ALREADY_CLAIMED",
                        "a master has already claimed the authoritative order; late quote confirmation cannot replace it",
                        409,
                        {
                            "claimedOrderId": order["orderId"],
                            "claimedQuoteId": order["quoteId"],
                            "claimedBy": order.get("claimedBy"),
                            "measurementVersionId": order.get("measurementVersionId"),
                            "claimedAt": order.get("claimedAt"),
                        },
                    )

            if not quote.get("feasible"):
                raise ServiceError("QUOTE_NOT_FEASIBLE", "cannot confirm an infeasible quote", 409)

            oid = quote.get("orderId") or self._next_id("ord")
            sheet = copy.deepcopy(quote["stepSheet"])
            sheet["status"] = "confirmed"
            order = {
                "orderId": oid,
                "version": 1,
                "status": "confirmed",
                "lockKey": lock_key,
                "quoteId": qid,
                "quoteVersion": 1,
                "customerId": payload.get("customerId") or quote.get("customerId"),
                "confirmedAt": time.time(),
                "confirmation": {
                    "by": payload.get("confirmedBy") or payload.get("customerId") or "customer",
                    "at": time.time(),
                    "idempotencyKey": idem,
                    "customerNote": payload.get("note", ""),
                },
                "floorPlanId": quote["floorPlanId"],
                "measurementVersionId": quote.get("measurementVersionId"),
                "measurement": quote.get("measurement"),
                "price": quote["price"],
                "orderRequest": copy.deepcopy(quote["orderRequest"]),
                "stepSheet": sheet,
                "completedStepIndex": 0,
                "issuedThroughIndex": 0,
                "claimToken": None,
                "claimedBy": None,
                "claimedAt": None,
                "reports": [],
                "provenance": {
                    "quoteId": qid,
                    "catalogRevision": quote.get("catalogRevisionAtQuote"),
                    "catalogHash": quote.get("catalogHashAtQuote"),
                    "floorPlanHashAtQuote": quote.get("floorPlanHashAtQuote"),
                    "measurementVersionId": quote.get("measurementVersionId"),
                },
            }
            self.orders[oid] = order
            quote["status"] = "confirmed"
            quote["orderId"] = oid
            quote["confirmation"] = order["confirmation"]
            self.quotes[qid] = quote
            self._persist_locked()
            return copy.deepcopy(order)

    def claim_order(self, oid: str, payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        payload = payload or {}
        with self.lock:
            order = self._require_order_locked(oid)
            master = payload.get("masterId") or payload.get("master") or "master-unknown"
            token = payload.get("claimToken")
            if order["status"] in ("claimed", "in_progress"):
                if order.get("claimedBy") == master or (token and secrets.compare_digest(token, order.get("claimToken") or "")):
                    return self._order_with_current_step_locked(order)
                raise ServiceError(
                    "ORDER_ALREADY_CLAIMED",
                    f"order is already claimed by {order.get('claimedBy')}",
                    409,
                    {"claimedBy": order.get("claimedBy")},
                )
            if order["status"] == "completed":
                raise ServiceError("ORDER_COMPLETED", "completed order cannot be claimed again", 409)

            order["status"] = "claimed"
            order["claimedBy"] = master
            order["claimedAt"] = time.time()
            order["claimToken"] = secrets.token_hex(12)
            order["version"] = int(order.get("version", 1)) + 1
            validation = self.revalidate_order(oid)
            if validation["valid"]:
                order["issuedThroughIndex"] = 1
                order["stepSheet"]["status"] = "active"
            else:
                order["issuanceBlocked"] = True
                order["stepSheet"]["status"] = "stale"
            self._persist_locked()
            return self._order_with_current_step_locked(order)

    def _require_order_locked(self, oid: str) -> Dict[str, Any]:
        if oid not in self.orders:
            raise ServiceError("ORDER_NOT_FOUND", oid, 404)
        return self.orders[oid]

    def _auth_claim_locked(self, order: Dict[str, Any], payload: Dict[str, Any], headers: Optional[Dict[str, str]] = None) -> None:
        headers = headers or {}
        token = payload.get("claimToken") or headers.get("x-claim-token")
        master = payload.get("masterId") or headers.get("x-master-id")
        if not token and not master:
            # The local single-shop service is permissive by default; claim
            # ownership is still recorded and a token is issued for clients
            # that want to present it.
            return
        if order.get("claimToken") and token and secrets.compare_digest(str(token), str(order["claimToken"])):
            return
        if master and order.get("claimedBy") == master:
            return
        raise ServiceError("CLAIM_AUTH_REQUIRED", "provide the claim token or claiming masterId", 403)

    def _order_with_current_step_locked(self, order: Dict[str, Any]) -> Dict[str, Any]:
        out = copy.deepcopy(order)
        idx = int(order.get("issuedThroughIndex", 0))
        steps = order["stepSheet"]["steps"]
        out["currentStep"] = steps[idx - 1] if 1 <= idx <= len(steps) else None
        next_idx = int(order.get("completedStepIndex", 0)) + 1
        out["nextStep"] = steps[next_idx - 1] if next_idx <= len(steps) and order["stepSheet"].get("status") == "active" else None
        out["claimToken"] = order.get("claimToken") if order.get("claimToken") else out.get("claimToken")
        return out

    def get_order(self, oid: str) -> Dict[str, Any]:
        with self.lock:
            return self._order_with_current_step_locked(self._require_order_locked(oid))

    def get_provenance(self, oid: str) -> Dict[str, Any]:
        with self.lock:
            order = self._require_order_locked(oid)
            measurement = None
            if order.get("measurementVersionId"):
                measurement = self.get_measurement(order["measurementVersionId"])
            return {
                "orderId": oid,
                **copy.deepcopy(order["provenance"]),
                "measurement": measurement,
                "quoteConfirmation": order.get("confirmation"),
                "claim": {
                    "claimedBy": order.get("claimedBy"),
                    "claimedAt": order.get("claimedAt"),
                    "version": order.get("version"),
                },
            }

    def revalidate_order(self, oid: str) -> Dict[str, Any]:
        with self.lock:
            order = self._require_order_locked(oid)
            fp = normalize_floorplan(self.floorplans[order["floorPlanId"]])
            result = replay_step_sheet(
                order["stepSheet"],
                fp,
                self.catalog,
                order["orderRequest"],
                strict_snapshot=True,
            )
            order["stepSheet"]["lastValidation"] = result
            if result["valid"]:
                if order["stepSheet"].get("status") != "completed":
                    order["stepSheet"]["status"] = "active" if order["status"] in ("claimed", "in_progress") else "ready"
                order["issuanceBlocked"] = False
            else:
                order["stepSheet"]["status"] = "stale"
                if order["status"] in ("claimed", "in_progress"):
                    order["issuanceBlocked"] = True
            self._persist_locked()
            return copy.deepcopy(result)

    def complete_step(
        self,
        oid: str,
        step_ref: str | int,
        payload: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        payload = payload or {}
        with self.lock:
            order = self._require_order_locked(oid)
            self._auth_claim_locked(order, payload, headers)
            if order["status"] == "completed":
                raise ServiceError("ORDER_COMPLETED", "all steps already reported", 409)
            if order["status"] not in ("claimed", "in_progress"):
                raise ServiceError("ORDER_NOT_CLAIMED", "master must claim before reporting steps", 409)

            steps = order["stepSheet"]["steps"]
            if isinstance(step_ref, int) or str(step_ref).isdigit():
                target_index = int(step_ref)
            else:
                target_index = next((i for i, s in enumerate(steps, 1) if s["stepId"] == step_ref), None)  # type: ignore
            if target_index is None or not (1 <= target_index <= len(steps)):
                raise ServiceError("STEP_NOT_FOUND", f"unknown step {step_ref}", 404)

            expected_index = int(order.get("completedStepIndex", 0)) + 1
            if target_index != expected_index and not payload.get("force"):
                raise ServiceError(
                    "STEP_ORDER_VIOLATION",
                    f"next reportable step is {expected_index}, got {target_index}",
                    409,
                    {"nextStepIndex": expected_index},
                )

            fp = normalize_floorplan(self.floorplans[order["floorPlanId"]])
            validation = replay_step_sheet(
                order["stepSheet"], fp, self.catalog, order["orderRequest"], strict_snapshot=True
            )
            first_bad = validation.get("firstInvalidStepIndex")

            # Cannot sign a step if current geometry/craft already invalidates it.
            if first_bad is not None and first_bad <= target_index:
                order["stepSheet"]["status"] = "stale"
                order["issuanceBlocked"] = True
                self._persist_locked()
                raise ServiceError(
                    "STEP_SHEET_INVALID",
                    "step sheet failed re-validation; no further compliant step may be issued",
                    409,
                    validation,
                )

            # Record the physically completed report.
            order["completedStepIndex"] = target_index
            order["status"] = "in_progress"
            order["reports"].append(
                {
                    "stepIndex": target_index,
                    "stepId": steps[target_index - 1]["stepId"],
                    "reportedAt": time.time(),
                    "by": payload.get("masterId") or order.get("claimedBy"),
                    "note": payload.get("note", ""),
                }
            )

            response: Dict[str, Any] = {
                "accepted": True,
                "completedStepIndex": target_index,
                "orderId": oid,
            }

            if first_bad is not None:
                # Prefix was valid, but a future template/clearance is blocked.
                order["stepSheet"]["status"] = "repair_required"
                order["issuanceBlocked"] = True
                response.update(
                    {
                        "nextStep": None,
                        "canIssueNext": False,
                        "code": "FUTURE_STEP_INVALID",
                        "validation": validation,
                    }
                )
            elif target_index == len(steps):
                order["status"] = "completed"
                order["stepSheet"]["status"] = "completed"
                order["completedAt"] = time.time()
                response.update({"nextStep": None, "canIssueNext": False, "orderStatus": "completed"})
            else:
                order["issuedThroughIndex"] = target_index + 1
                order["stepSheet"]["status"] = "active"
                response.update(
                    {
                        "nextStep": steps[target_index],  # zero-based target_index is next step
                        "canIssueNext": True,
                        "validation": {"valid": True, "floorPlanHash": validation["floorPlanHash"]},
                    }
                )
            self._persist_locked()
            return copy.deepcopy(response)

    def issue_next_step(
        self,
        oid: str,
        payload: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        payload = payload or {}
        with self.lock:
            order = self._require_order_locked(oid)
            self._auth_claim_locked(order, payload, headers)
            if order["status"] not in ("claimed", "in_progress"):
                raise ServiceError("ORDER_NOT_ACTIVE", f"order status is {order['status']}", 409)
            next_index = int(order.get("completedStepIndex", 0)) + 1
            steps = order["stepSheet"]["steps"]
            if next_index > len(steps):
                raise ServiceError("ORDER_COMPLETED", "no more steps", 409)
            validation = self.revalidate_order(oid)
            if not validation["valid"]:
                order["stepSheet"]["status"] = "stale"
                order["issuanceBlocked"] = True
                self._persist_locked()
                raise ServiceError(
                    "STEP_SHEET_INVALID",
                    "step sheet failed current re-validation; no next step may be issued",
                    409,
                    validation,
                )
            order["issuedThroughIndex"] = next_index
            order["stepSheet"]["status"] = "active"
            self._persist_locked()
            return {
                "issued": True,
                "stepIndex": next_index,
                "step": copy.deepcopy(steps[next_index - 1]),
                "claimToken": order.get("claimToken"),
            }

    def generic_replay(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        with self.lock:
            sheet = payload.get("stepSheet")
            if not sheet and payload.get("quoteId"):
                sheet = self.get_quote(payload["quoteId"])["stepSheet"]
            if not sheet and payload.get("orderId"):
                sheet = self._require_order_locked(payload["orderId"])["stepSheet"]
            if not sheet:
                raise ServiceError("SHEET_REQUIRED", "provide stepSheet, quoteId or orderId")
            fp_data = payload.get("floorPlan")
            if not fp_data and payload.get("floorPlanId"):
                fp_data = self.get_floorplan(payload["floorPlanId"])
                # get_floorplan returns normalized view; it can be normalized again.
            if not fp_data:
                fp_data = self.floorplans.get(sheet.get("floorPlanId", ""))
            if not fp_data:
                raise ServiceError("FLOORPLAN_REQUIRED", "provide floorPlan/floorPlanId")
            catalog = self.catalog
            if payload.get("catalog"):
                catalog = payload["catalog"]
            order_request = payload.get("orderRequest")
            if payload.get("orderId") and not order_request:
                order_request = self._require_order_locked(payload["orderId"])["orderRequest"]
            return replay_step_sheet(
                sheet,
                fp_data,
                catalog,
                order_request,
                strict_snapshot=bool(payload.get("strictSnapshot", True)),
            )
