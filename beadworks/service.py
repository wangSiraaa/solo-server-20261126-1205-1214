from __future__ import annotations

"""Core counter workflow for beaded chains.

The important rule is physical, not bookkeeping-only: a piece can be removed
only when a complete slide path reaches a *fitted, openable* port, every fixed
ring on that path fits over the moving bead, and every ordinary bead that must
be cleared has an in-shop / temporary-removal record.
"""

import threading
import uuid
from datetime import date, datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from .store import JsonStore


class BeadworksError(Exception):
    status = 400
    code = "bad_request"

    def __init__(self, message: str, details: Optional[Dict[str, Any]] = None):
        super().__init__(message)
        self.message = message
        self.details = details or {}

    def to_dict(self) -> Dict[str, Any]:
        return {"error": self.code, "message": self.message, "details": self.details}


class ValidationError(BeadworksError):
    status = 400
    code = "validation_error"


class NotFoundError(BeadworksError):
    status = 404
    code = "not_found"


class ConflictError(BeadworksError):
    status = 409
    code = "conflict"


class RemovalNotFeasible(ConflictError):
    code = "removal_not_feasible"

    def __init__(self, report: Dict[str, Any]):
        super().__init__("该珠目前不能从链上单独拆还", report)
        self.report = report


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _as_date(value: Any) -> str:
    if isinstance(value, date):
        return value.isoformat()
    if not isinstance(value, str):
        raise ValidationError("日期必须是 YYYY-MM-DD 字符串")
    try:
        date.fromisoformat(value)
    except ValueError as exc:
        raise ValidationError("日期必须是 YYYY-MM-DD 字符串") from exc
    return value


def _number(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValidationError(f"{field} 必须是数字")
    if value <= 0:
        raise ValidationError(f"{field} 必须大于零")
    return float(value)


class Beadworks:
    def __init__(self, path: str = ":memory:"):
        self.store = JsonStore(path)
        self.db = self.store.state
        self.lock = threading.RLock()

    # ---------- generic helpers ----------

    def commit(self) -> None:
        self.store.save()

    @staticmethod
    def _copy(value: Any) -> Any:
        import copy

        return copy.deepcopy(value)

    def _new_id(self, prefix: str, supplied: Optional[str] = None) -> str:
        if supplied:
            return supplied
        n = int(self.db["counters"].get(prefix, 0)) + 1
        self.db["counters"][prefix] = n
        return f"{prefix}_{n:04d}"

    def _event(self, event_type: str, **payload: Any) -> None:
        event = {"id": f"evt_{uuid.uuid4().hex[:12]}", "type": event_type, "at": _now()}
        event.update(payload)
        self.db["events"].append(event)

    def _require(self, collection: str, item_id: str) -> Dict[str, Any]:
        item = self.db[collection].get(item_id)
        if item is None:
            raise NotFoundError(f"{collection[:-1] if collection.endswith('s') else collection} 不存在: {item_id}")
        return item

    def state(self) -> Dict[str, Any]:
        with self.lock:
            return self.store.snapshot()

    # ---------- people and intake ----------

    def create_person(
        self,
        name: str,
        person_id: Optional[str] = None,
        roles: Optional[Sequence[str]] = None,
        note: Optional[str] = None,
    ) -> Dict[str, Any]:
        with self.lock:
            if not name or not str(name).strip():
                raise ValidationError("name 不能为空")
            pid = self._new_id("person", person_id)
            if pid in self.db["people"]:
                raise ConflictError(f"人员编号已存在: {pid}")
            person = {
                "id": pid,
                "name": name.strip(),
                "roles": list(roles or []),
                "note": note,
                "createdAt": _now(),
            }
            self.db["people"][pid] = person
            self._event("person_created", personId=pid)
            self.commit()
            return self._copy(person)

    def receive_piece(
        self,
        name: str,
        owner_id: str,
        hole_width: Any,
        piece_id: Optional[str] = None,
        kind: str = "bead",
        color: Optional[str] = None,
        temp_removal_allowed: bool = True,
        consigned_for_rental: bool = False,
        authorized_recipient_ids: Optional[Sequence[str]] = None,
        intake_note: Optional[str] = None,
        at: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Register a customer's piece and its intake records.

        ``temp_removal_allowed`` is deliberately explicit: every bead cleared
        from a slide path must have a recorded yes/no decision.
        """
        with self.lock:
            owner = self._require("people", owner_id)
            hole = _number(hole_width, "hole_width")
            pid = self._new_id("piece", piece_id)
            if pid in self.db["pieces"]:
                raise ConflictError(f"饰品编号已存在: {pid}")
            for rid in authorized_recipient_ids or []:
                self._require("people", rid)

            now = at or _now()
            piece = {
                "id": pid,
                "name": name,
                "kind": kind,
                "color": color,
                "holeWidth": hole,
                "ownerId": owner["id"],
                "authorizedRecipientIds": list(authorized_recipient_ids or []),
                "consignedForRental": bool(consigned_for_rental),
                "tempRemovalAllowed": bool(temp_removal_allowed),
                "inShop": True,
                "location": "shop",
                "status": "in_shop",
                "holderId": None,
                "chainId": None,
                "activeRentalId": None,
                "lastIndividualReturnId": None,
                "createdAt": now,
            }
            self.db["pieces"][pid] = piece
            intake = {
                "id": self._new_id("intake"),
                "pieceId": pid,
                "at": now,
                "inShop": True,
                "tempRemovalAllowed": bool(temp_removal_allowed),
                "note": intake_note,
            }
            self.db["intakes"].append(intake)
            self._event("piece_received", pieceId=pid, ownerId=owner["id"])
            self.commit()
            return self.get_piece(pid)

    def get_piece(self, piece_id: str) -> Dict[str, Any]:
        with self.lock:
            return self._copy(self._require("pieces", piece_id))

    def list_pieces(self) -> List[Dict[str, Any]]:
        with self.lock:
            return self._copy(list(self.db["pieces"].values()))

    # ---------- clasps ----------

    def add_clasp(
        self,
        size: Any,
        clasp_id: Optional[str] = None,
        name: Optional[str] = None,
        kind: str = "shop",
        required_holder_id: Optional[str] = None,
        shop_controlled: Optional[bool] = None,
        fitted: bool = False,
        source: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Add a clasp.

        A catalog clasp is ``fitted=False`` and is only available hardware.
        It becomes an exit only after :meth:`install_clasp` or clerk
        registration of a clasp already physically on the chain.
        """
        with self.lock:
            if kind not in {"shop", "sealed", "personal", "standard"}:
                raise ValidationError("kind 必须是 shop/sealed/personal/standard")
            size_value = _number(size, "size")
            cid = self._new_id("clasp", clasp_id)
            if cid in self.db["clasps"]:
                raise ConflictError(f"扣编号已存在: {cid}")
            if required_holder_id:
                self._require("people", required_holder_id)
            if kind == "sealed" and not required_holder_id:
                raise ValidationError("压封扣必须指定 required_holder_id")
            controlled = shop_controlled if shop_controlled is not None else kind == "shop"
            if kind == "sealed":
                # A pressure seal can never be bypassed by a normal shop key.
                controlled = False
            clasp = {
                "id": cid,
                "name": name or f"{kind}-clasp-{cid}",
                "kind": kind,
                "size": size_value,
                "fitted": bool(fitted),
                "shopControlled": bool(controlled),
                "requiredHolderId": required_holder_id,
                "chainId": None,
                "installedByShop": bool(fitted and source == "shop_installed"),
                "source": source or ("clerk_registered" if fitted else "catalog"),
                "createdAt": _now(),
            }
            self.db["clasps"][cid] = clasp
            self._event("clasp_added", claspId=cid, fitted=fitted)
            self.commit()
            return self._copy(clasp)

    def stock_clasp(self, size: Any, **kwargs: Any) -> Dict[str, Any]:
        kwargs["fitted"] = False
        kwargs["source"] = "catalog"
        return self.add_clasp(size, **kwargs)

    def list_clasps(self) -> List[Dict[str, Any]]:
        with self.lock:
            return self._copy(list(self.db["clasps"].values()))

    def install_clasp(
        self,
        chain_id: str,
        clasp_id: str,
        segment_id: str,
        endpoint: str,
        second_segment_id: Optional[str] = None,
        second_endpoint: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Actually fit a stocked clasp to one or two chain endpoints."""
        with self.lock:
            chain = self._require("chains", chain_id)
            clasp = self._require("clasps", clasp_id)
            if endpoint not in {"left", "right"}:
                raise ValidationError("endpoint 必须是 left 或 right")
            if clasp["fitted"] and clasp.get("chainId") != chain_id:
                raise ConflictError("该扣已经配在另一条链上")
            if clasp["fitted"] and clasp.get("chainId") == chain_id:
                raise ConflictError("该扣已经装在本链上，不能重复加装")

            segment = self._segment(chain, segment_id)
            if segment[endpoint]["type"] == "port":
                raise ConflictError(f"{segment_id}.{endpoint} 已有出口，不能在原处加装")
            segment[endpoint] = {"type": "port", "claspId": clasp_id}

            if second_segment_id:
                other_end = second_endpoint or ("left" if endpoint == "right" else "right")
                other = self._segment(chain, second_segment_id)
                if other[other_end]["type"] == "port":
                    raise ConflictError(f"{second_segment_id}.{other_end} 已有出口，不能连接")
                other[other_end] = {"type": "port", "claspId": clasp_id}

            clasp["fitted"] = True
            clasp["chainId"] = chain_id
            clasp["installedByShop"] = True
            clasp["source"] = "shop_installed"
            clasp["installedAt"] = _now()
            self._event(
                "clasp_installed",
                chainId=chain_id,
                claspId=clasp_id,
                segmentId=segment_id,
                endpoint=endpoint,
                secondSegmentId=second_segment_id,
            )
            self.commit()
            return self.get_chain_view(chain_id)

    def install_connecting_clasp(
        self, chain_id: str, clasp_id: str, left_segment_id: str, right_segment_id: str
    ) -> Dict[str, Any]:
        with self.lock:
            chain = self._require("chains", chain_id)
            ids = [s["id"] for s in chain["segments"]]
            if left_segment_id not in ids or right_segment_id not in ids:
                raise NotFoundError("链段不存在")
            if ids.index(left_segment_id) + 1 != ids.index(right_segment_id):
                raise ValidationError("连接扣只能装在相邻的左右链段之间")
            return self.install_clasp(
                chain_id,
                clasp_id,
                left_segment_id,
                "right",
                second_segment_id=right_segment_id,
                second_endpoint="left",
            )

    # ---------- chains ----------

    @staticmethod
    def _segment(chain: Dict[str, Any], segment_id: str) -> Dict[str, Any]:
        for segment in chain["segments"]:
            if segment["id"] == segment_id:
                return segment
        raise NotFoundError(f"链段不存在: {segment_id}")

    def create_chain(
        self,
        segments: Sequence[Dict[str, Any]],
        chain_id: Optional[str] = None,
        label: Optional[str] = None,
        style: str = "registered",
        at_shop: bool = True,
        designed_by: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Register the clerk's record of segment order and existing clasps."""
        with self.lock:
            cid = self._new_id("chain", chain_id)
            if cid in self.db["chains"]:
                raise ConflictError(f"链编号已存在: {cid}")
            if not segments:
                raise ValidationError("segments 至少包含一段")

            normalized_segments: List[Dict[str, Any]] = []
            seen_pieces = set()
            for s_index, raw in enumerate(segments):
                sid = raw.get("id") or f"{cid}_seg{s_index + 1}"
                track: List[Dict[str, Any]] = []
                for t_index, item in enumerate(raw.get("track", [])):
                    itype = item.get("type")
                    if itype == "bead":
                        pid = item.get("pieceId")
                        piece = self._require("pieces", pid)
                        if pid in seen_pieces:
                            raise ValidationError(f"饰品在同一条链上重复: {pid}")
                        seen_pieces.add(pid)
                        if piece.get("chainId"):
                            raise ConflictError(f"饰品已经在另一条链上: {pid}")
                        track.append({"type": "bead", "pieceId": pid})
                    elif itype == "ring":
                        width = _number(item.get("width"), "ring.width")
                        rid = item.get("ringId") or self._new_id("ring")
                        track.append(
                            {
                                "type": "ring",
                                "ringId": rid,
                                "width": width,
                                "fixed": bool(item.get("fixed", True)),
                                "label": item.get("label"),
                            }
                        )
                    else:
                        raise ValidationError("track item type 必须是 bead 或 ring")

                left = self._normalize_endpoint(raw.get("left", {"type": "closed"}), cid)
                right = self._normalize_endpoint(raw.get("right", {"type": "closed"}), cid)
                normalized_segments.append(
                    {"id": sid, "label": raw.get("label"), "track": track, "left": left, "right": right}
                )

            chain = {
                "id": cid,
                "label": label or cid,
                "style": style,
                "designedBy": designed_by,
                "segments": normalized_segments,
                "inShop": at_shop,
                "location": "shop" if at_shop else "outside",
                "holderId": None,
                "createdAt": _now(),
            }
            # Validate all referenced physical clasps before publishing/mutating.
            for segment in normalized_segments:
                for end_key in ("left", "right"):
                    end = segment[end_key]
                    if end["type"] == "port":
                        clasp = self._require("clasps", end["claspId"])
                        if not clasp.get("fitted"):
                            raise ConflictError(
                                f"扣 {end['claspId']} 仍在目录/库存中，未实际配上，不能算作出口"
                            )
                        if clasp.get("chainId") and clasp["chainId"] != cid:
                            raise ConflictError(f"扣 {end['claspId']} 已配在另一条链上")

            self.db["chains"][cid] = chain

            # Finalize references after validation succeeds.
            for segment in normalized_segments:
                for end_key in ("left", "right"):
                    end = segment[end_key]
                    if end["type"] == "port":
                        self.db["clasps"][end["claspId"]]["chainId"] = cid
            for pid in seen_pieces:
                piece = self.db["pieces"][pid]
                piece["chainId"] = cid
                piece["status"] = "on_chain"
                piece["inShop"] = at_shop
                piece["location"] = "shop" if at_shop else "outside"
                if at_shop:
                    # Counter has physical custody even if a loan contract remains
                    # open until it is settled/returned.
                    piece["holderId"] = None

            self._event("chain_registered", chainId=cid, pieceIds=sorted(seen_pieces))
            self.commit()
            return self.get_chain_view(cid)

    def _normalize_endpoint(self, end: Dict[str, Any], chain_id: str) -> Dict[str, Any]:
        etype = end.get("type", "closed")
        if etype == "closed":
            return {"type": "closed", "openable": bool(end.get("openable", False))}
        if etype == "port":
            cid = end.get("claspId")
            if not cid:
                raise ValidationError("port 端点必须提供 claspId")
            # Existence/fitted is checked in create_chain after all refs normalize.
            return {"type": "port", "claspId": cid}
        raise ValidationError("endpoint type 必须是 closed 或 port")

    def get_chain(self, chain_id: str) -> Dict[str, Any]:
        with self.lock:
            return self._copy(self._require("chains", chain_id))

    def list_chains(self) -> List[Dict[str, Any]]:
        with self.lock:
            return self._copy(list(self.db["chains"].values()))

    def current_piece_ids(self, chain_id: str) -> List[str]:
        chain = self._require("chains", chain_id)
        ids: List[str] = []
        for segment in chain["segments"]:
            for item in segment["track"]:
                if item["type"] == "bead":
                    ids.append(item["pieceId"])
        return ids

    def get_chain_view(self, chain_id: str) -> Dict[str, Any]:
        with self.lock:
            chain = self._require("chains", chain_id)
            piece_ids = self.current_piece_ids(chain_id)
            clasp_ids: List[str] = []
            connections: Dict[str, List[Dict[str, str]]] = {}
            for segment in chain["segments"]:
                for endpoint in ("left", "right"):
                    end = segment[endpoint]
                    if end["type"] == "port":
                        cid = end["claspId"]
                        if cid not in clasp_ids:
                            clasp_ids.append(cid)
                        connections.setdefault(cid, []).append(
                            {"segmentId": segment["id"], "endpoint": endpoint}
                        )
            clasp_views = [self._copy(self._require("clasps", cid)) for cid in clasp_ids]
            view = self._copy(chain)
            view["currentPieceIds"] = piece_ids
            view["attachedClaspIds"] = clasp_ids
            view["remainingClaspIds"] = clasp_ids
            view["claspConnections"] = connections
            view["clasps"] = clasp_views
            return view

    # ---------- try-fit ----------

    def try_fit(self, hole_width: Any, ring_width: Any) -> Dict[str, Any]:
        hole = _number(hole_width, "hole_width")
        ring = _number(ring_width, "ring_width")
        fits = ring <= hole
        return {
            "holeWidth": hole,
            "ringWidth": ring,
            "fits": fits,
            "code": "PASS" if fits else "RING_BLOCKS_HOLE",
            "message": "圈不宽于珠孔，可以越过" if fits else "圈宽超过珠孔，挡住",
        }

    def try_fit_piece(self, piece_id: str, ring_width: Any) -> Dict[str, Any]:
        with self.lock:
            piece = self._require("pieces", piece_id)
            result = self.try_fit(piece["holeWidth"], ring_width)
            result["pieceId"] = piece_id
            return result

    # ---------- visits and consent ----------

    def schedule_visit(
        self, person_id: str, on_date: Any, purpose: Optional[str] = None, visit_id: Optional[str] = None
    ) -> Dict[str, Any]:
        with self.lock:
            day = _as_date(on_date)
            self._require("people", person_id)
            vid = self._new_id("visit", visit_id)
            if vid in self.db["visits"]:
                raise ConflictError(f"到柜预约编号已存在: {vid}")
            visit = {
                "id": vid,
                "personId": person_id,
                "date": day,
                "purpose": purpose,
                "status": "scheduled",
                "createdAt": _now(),
                "arrivedAt": None,
            }
            self.db["visits"][vid] = visit
            self.commit()
            return self._copy(visit)

    def check_in_visit(self, visit_id: str) -> Dict[str, Any]:
        with self.lock:
            visit = self._require("visits", visit_id)
            visit["status"] = "arrived"
            visit["arrivedAt"] = _now()
            self._event("visit_arrived", visitId=visit_id, personId=visit["personId"])
            self.commit()
            return self._copy(visit)

    def mark_visit_absent(self, visit_id: str) -> Dict[str, Any]:
        with self.lock:
            visit = self._require("visits", visit_id)
            visit["status"] = "absent"
            visit["markedAbsentAt"] = _now()
            self._event("visit_absent", visitId=visit_id, personId=visit["personId"])
            self.commit()
            return self._copy(visit)

    def grant_consent(
        self,
        person_id: str,
        clasp_id: str,
        on_date: Any,
        visit_id: Optional[str] = None,
        agreed: bool = True,
        consent_id: Optional[str] = None,
        scope: str = "open_clasp",
    ) -> Dict[str, Any]:
        """Record consent only against an actual arrived counter visit."""
        with self.lock:
            day = _as_date(on_date)
            person = self._require("people", person_id)
            clasp = self._require("clasps", clasp_id)
            if visit_id is None:
                matches = [
                    v
                    for v in self.db["visits"].values()
                    if v["personId"] == person_id and v["date"] == day and v["status"] == "arrived"
                ]
                if not matches:
                    raise ConflictError("持有人未实际到柜，不能记录有效同意")
                visit = matches[-1]
            else:
                visit = self._require("visits", visit_id)
                if visit["personId"] != person_id:
                    raise ValidationError("预约与同意人不一致")
                if visit["date"] != day:
                    raise ValidationError("同意日期与到柜日期不一致")
                if visit["status"] != "arrived":
                    raise ConflictError("持有人未实际到柜，不能记录有效同意")

            cid = self._new_id("consent", consent_id)
            if cid in self.db["consents"]:
                raise ConflictError(f"同意记录已存在: {cid}")
            consent = {
                "id": cid,
                "personId": person["id"],
                "claspId": clasp_id,
                "visitId": visit["id"],
                "date": day,
                "businessDate": day,
                "scope": scope,
                "agreed": bool(agreed),
                "createdAt": _now(),
            }
            self.db["consents"][cid] = consent
            self._event("consent_recorded", consentId=cid, claspId=clasp_id, agreed=agreed)
            self.commit()
            return self._copy(consent)

    def open_clasp(
        self,
        clasp_id: str,
        on_date: Any,
        consent_id: Optional[str] = None,
        opening_id: Optional[str] = None,
        chain_id: Optional[str] = None,
        purpose: str = "counter_service",
    ) -> Dict[str, Any]:
        """Validate presence/consent and create an audited clasp-opening record."""
        with self.lock:
            day = _as_date(on_date)
            clasp = self._require("clasps", clasp_id)
            consent_ids = [consent_id] if consent_id else None
            auth = self._clasp_authorization(clasp, day, consent_ids)
            if not auth["authorized"]:
                raise ConflictError(auth["message"], auth)
            oid = self._new_id("opening", opening_id)
            if oid in self.db["clasp_openings"]:
                raise ConflictError(f"开扣记录已存在: {oid}")
            opening = {
                "id": oid,
                "claspId": clasp_id,
                "chainId": chain_id or clasp.get("chainId"),
                "date": day,
                "purpose": purpose,
                "authorizationCode": auth["code"],
                "consentId": auth.get("consentId"),
                "requiredHolderId": auth.get("requiredHolderId"),
                "openedAt": _now(),
            }
            self.db["clasp_openings"][oid] = opening
            self._event("clasp_opened", **opening)
            self.commit()
            return self._copy(opening)

    def record_handling_permission(
        self,
        piece_id: str,
        temp_removal_allowed: bool,
        in_shop: Optional[bool] = None,
        note: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Add a later yes/no handling record for an intake piece."""
        with self.lock:
            piece = self._require("pieces", piece_id)
            piece["tempRemovalAllowed"] = bool(temp_removal_allowed)
            if in_shop is not None:
                piece["inShop"] = bool(in_shop)
                piece["location"] = "shop" if in_shop else "outside"
            record = {
                "id": self._new_id("intake"),
                "pieceId": piece_id,
                "at": _now(),
                "inShop": piece["inShop"],
                "tempRemovalAllowed": bool(temp_removal_allowed),
                "note": note or "柜台处理权限更新",
            }
            self.db["intakes"].append(record)
            self.commit()
            return self._copy(record)

    def _clasp_authorization(
        self, clasp: Dict[str, Any], day: str, consent_ids: Optional[Sequence[str]] = None
    ) -> Dict[str, Any]:
        if not clasp.get("fitted"):
            return {"authorized": False, "code": "CLASP_NOT_FITTED", "message": "目录里的扣尚未实际配上，不是出口"}
        if clasp.get("kind") != "sealed" and clasp.get("shopControlled"):
            return {
                "authorized": True,
                "code": "SHOP_CLASP",
                "message": "店里有权开启的扣",
                "claspId": clasp["id"],
            }

        holder_id = clasp.get("requiredHolderId")
        if not holder_id:
            return {
                "authorized": False,
                "code": "NO_RESPONSIBLE_HOLDER",
                "message": "非店控扣没有登记可授权持有人",
                "claspId": clasp["id"],
            }

        allowed_ids = set(consent_ids or [])
        consent = None
        for candidate in self.db["consents"].values():
            if candidate["claspId"] != clasp["id"]:
                continue
            if candidate.get("businessDate") != day:
                continue
            if allowed_ids and candidate["id"] not in allowed_ids:
                continue
            visit = self.db["visits"].get(candidate["visitId"])
            if candidate.get("agreed") and visit and visit.get("status") == "arrived":
                if visit.get("personId") == holder_id and visit.get("date") == day:
                    consent = candidate
                    break

        if consent:
            return {
                "authorized": True,
                "code": "HOLDER_CONSENT",
                "message": "相应持有人已到柜并同意",
                "claspId": clasp["id"],
                "requiredHolderId": holder_id,
                "consentId": consent["id"],
            }

        holder = self.db["people"].get(holder_id, {})
        visits = [v for v in self.db["visits"].values() if v["personId"] == holder_id and v["date"] == day]
        if visits and visits[-1].get("status") == "absent":
            code, message = "HOLDER_ABSENT", f"封扣需 {holder.get('name', holder_id)} 到场，但其当天缺席"
        elif visits and visits[-1].get("status") == "scheduled":
            code, message = "HOLDER_NOT_CHECKED_IN", f"封扣需 {holder.get('name', holder_id)} 实际到柜签到"
        elif any(v.get("status") == "arrived" for v in visits):
            code, message = "CONSENT_MISSING", "持有人已到柜，但未同意开扣"
        else:
            code, message = "HOLDER_ABSENT", f"封扣需 {holder.get('name', holder_id)} 当天到柜同意"
        return {
            "authorized": False,
            "code": code,
            "message": message,
            "claspId": clasp["id"],
            "requiredHolderId": holder_id,
            "requiredHolderName": holder.get("name"),
        }

    # ---------- removal planning / quotes ----------

    def _locate_on_chain(self, chain: Dict[str, Any], piece_id: str) -> Optional[Tuple[int, int]]:
        for si, segment in enumerate(chain["segments"]):
            for ti, item in enumerate(segment["track"]):
                if item["type"] == "bead" and item["pieceId"] == piece_id:
                    return si, ti
        return None

    def plan_removal(
        self,
        chain_id: str,
        piece_id: str,
        on_date: Any,
        consent_ids: Optional[Sequence[str]] = None,
    ) -> Dict[str, Any]:
        with self.lock:
            day = _as_date(on_date)
            chain = self._require("chains", chain_id)
            piece = self._require("pieces", piece_id)
            loc = self._locate_on_chain(chain, piece_id)
            common: Dict[str, Any] = {
                "chainId": chain_id,
                "pieceId": piece_id,
                "date": day,
                "improvisedClaspAllowed": False,
            }
            if loc is None or piece.get("chainId") != chain_id:
                common.update(
                    {
                        "feasible": False,
                        "blockers": [{"code": "PIECE_NOT_ON_CHAIN", "message": "该饰品当前不在这条链上"}],
                        "routes": [],
                        "chosenDirection": None,
                        "canMeetDueDate": False,
                    }
                )
                return common

            si, ti = loc
            segment = chain["segments"][si]
            routes: List[Dict[str, Any]] = []
            for direction in ("left", "right"):
                routes.append(
                    self._plan_direction(chain, segment, ti, direction, piece, day, consent_ids or [])
                )

            custody_blockers: List[Dict[str, Any]] = []
            if not piece.get("inShop") or piece.get("location") not in {"shop", "shop_temp_tray"}:
                custody_blockers.append(
                    {
                        "code": "PIECE_NOT_IN_SHOP",
                        "message": "目标珠当前不在柜台；只查在账不能替代实际接件",
                        "holderId": piece.get("holderId"),
                        "activeRentalId": piece.get("activeRentalId"),
                    }
                )
            if chain.get("inShop") is False or chain.get("location") not in {"shop", "shop_temp_tray"}:
                custody_blockers.append(
                    {
                        "code": "CHAIN_NOT_AT_COUNTER",
                        "message": "整条余链当前不在柜台，不能执行柜台拆还",
                        "holderId": chain.get("holderId"),
                    }
                )
            feasible_routes = [r for r in routes if not r["blockers"]]
            chosen = None
            if feasible_routes and not custody_blockers:
                chosen = sorted(
                    feasible_routes,
                    key=lambda r: (len(r["clearPieceIds"]), len(r["ringsCrossed"]), 0 if r["direction"] == "right" else 1),
                )[0]

            blockers: List[Dict[str, Any]] = list(custody_blockers)
            if chosen is None:
                for route in routes:
                    for blocker in route["blockers"]:
                        item = dict(blocker)
                        item["direction"] = route["direction"]
                        blockers.append(item)

            result = dict(common)
            pending_people = []
            for blocker in blockers:
                holder = blocker.get("requiredHolderId")
                if holder and holder not in pending_people:
                    pending_people.append(holder)
            result.update(
                {
                    "feasible": chosen is not None,
                    "canMeetDueDate": chosen is not None,
                    "routes": routes,
                    "chosenDirection": chosen["direction"] if chosen else None,
                    "chosenRoute": chosen,
                    "custodyBlockers": custody_blockers,
                    "blockers": blockers,
                    "requiredPeople": pending_people,
                    "summary": self._quote_summary(piece, chosen, blockers),
                }
            )
            return result

    def _plan_direction(
        self,
        chain: Dict[str, Any],
        segment: Dict[str, Any],
        target_index: int,
        direction: str,
        target_piece: Dict[str, Any],
        day: str,
        consent_ids: Sequence[str],
    ) -> Dict[str, Any]:
        track = segment["track"]
        if direction == "right":
            outward_indices = list(range(target_index + 1, len(track)))
            endpoint = segment["right"]
        else:
            outward_indices = list(range(target_index - 1, -1, -1))
            endpoint = segment["left"]

        blockers: List[Dict[str, Any]] = []
        rings_crossed: List[Dict[str, Any]] = []
        path_piece_ids: List[str] = []

        # Target itself must fit every fixed ring between its seat and the port.
        for idx in outward_indices:
            item = track[idx]
            if item["type"] == "ring":
                ring_info = {"ringId": item["ringId"], "width": item["width"]}
                rings_crossed.append(ring_info)
                if item["width"] > target_piece["holeWidth"]:
                    blockers.append(
                        {
                            "code": "RING_BLOCKS_HOLE",
                            "message": f"固定圈宽 {item['width']} 超过{target_piece['name']}珠孔 {target_piece['holeWidth']}",
                            **ring_info,
                        }
                    )
            else:
                path_piece_ids.append(item["pieceId"])

        # Clear order starts at the bead nearest the port and works inward.
        clear_indices = list(reversed(outward_indices))
        clear_piece_ids: List[str] = []
        for idx in clear_indices:
            item = track[idx]
            if item["type"] != "bead":
                continue
            pid = item["pieceId"]
            clear_piece_ids.append(pid)
            neighbor = self._require("pieces", pid)

            if not neighbor.get("tempRemovalAllowed"):
                blockers.append(
                    {
                        "code": "TEMP_REMOVAL_NOT_ALLOWED",
                        "message": f"邻珠 {neighbor.get('name', pid)} 没有允许临时取下的记录",
                        "pieceId": pid,
                    }
                )
            if not neighbor.get("inShop") or neighbor.get("location") not in {"shop", "shop_temp_tray"}:
                blockers.append(
                    {
                        "code": "NEIGHBOR_NOT_IN_SHOP",
                        "message": f"邻珠 {neighbor.get('name', pid)} 不在柜台，不能依次暂存",
                        "pieceId": pid,
                    }
                )
            if neighbor.get("activeRentalId"):
                blockers.append(
                    {
                        "code": "ACTIVE_RENTAL_PIECE",
                        "message": f"邻珠 {neighbor.get('name', pid)} 仍在有效租借中，不在店可操作状态",
                        "pieceId": pid,
                        "rentalId": neighbor["activeRentalId"],
                    }
                )

            # A cleared bead also has to reach the same port past rings between
            # its own seat and that port.
            if direction == "right":
                neighbor_outward = range(idx + 1, len(track))
            else:
                neighbor_outward = range(idx - 1, -1, -1)
            for j in neighbor_outward:
                other = track[j]
                if other["type"] == "ring" and other["width"] > neighbor["holeWidth"]:
                    blockers.append(
                        {
                            "code": "NEIGHBOR_RING_BLOCKS_HOLE",
                            "message": f"暂存邻珠 {neighbor.get('name', pid)} 无法越过宽 {other['width']} 的圈",
                            "pieceId": pid,
                            "ringId": other["ringId"],
                        }
                    )

        if endpoint["type"] != "port":
            authorization = {"authorized": False, "code": "NO_OPENABLE_PORT", "message": "该端不可打开，也没有实际配上的出口扣"}
            blockers.append({"code": "NO_OPENABLE_PORT", "message": authorization["message"]})
            clasp_id = None
        else:
            clasp_id = endpoint["claspId"]
            clasp = self._require("clasps", clasp_id)
            authorization = self._clasp_authorization(clasp, day, consent_ids)
            if not authorization["authorized"]:
                blockers.append(
                    {
                        "code": authorization["code"],
                        "message": authorization["message"],
                        "claspId": clasp_id,
                        "requiredHolderId": authorization.get("requiredHolderId"),
                    }
                )

        return {
            "direction": direction,
            "segmentId": segment["id"],
            "clearPieceIds": clear_piece_ids,
            "rethreadOrder": list(reversed(clear_piece_ids)),
            "pathPieceIds": path_piece_ids,
            "ringsCrossed": rings_crossed,
            "exitClaspId": clasp_id,
            "authorization": authorization,
            "blockers": blockers,
        }

    @staticmethod
    def _quote_summary(piece: Dict[str, Any], chosen: Optional[Dict[str, Any]], blockers: List[Dict[str, Any]]) -> str:
        if chosen:
            side = "右" if chosen["direction"] == "right" else "左"
            return (
                f"可从{side}端 {chosen['exitClaspId'] or '出口'} 取下{piece['name']}；"
                f"需暂存 {len(chosen['clearPieceIds'])} 件，越过 {len(chosen['ringsCrossed'])} 个圈。"
            )
        codes = "、".join(sorted({b["code"] for b in blockers})) or "UNKNOWN"
        return f"不能在该日单独取下{piece['name']}：{codes}。"

    def quote_removal(
        self,
        chain_id: str,
        piece_id: str,
        on_date: Any,
        quote_id: Optional[str] = None,
        consent_id: Optional[str] = None,
        consent_ids: Optional[Sequence[str]] = None,
    ) -> Dict[str, Any]:
        with self.lock:
            ids = list(consent_ids or [])
            if consent_id:
                ids.append(consent_id)
            report = self.plan_removal(chain_id, piece_id, on_date, ids)
            qid = self._new_id("quote", quote_id)
            report["id"] = qid
            report["kind"] = "removal_quote"
            report["orderCreated"] = False
            report["extraClaspInstalled"] = False
            self.db["quotes"][qid] = report
            self._event("removal_quoted", quoteId=qid, feasible=report["feasible"])
            self.commit()
            return self._copy(report)

    # ---------- segmented orders ----------

    def create_segmented_order(
        self,
        groups: Sequence[Dict[str, Any]],
        due_date: Any,
        order_id: Optional[str] = None,
        chain_id: Optional[str] = None,
        label: Optional[str] = None,
        connector_clasp_ids: Optional[Sequence[str]] = None,
        terminal_clasp_id: Optional[str] = None,
        customer_id: Optional[str] = None,
        pickup_schedule: Optional[Sequence[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        """Build the shop's chosen business model: independent short chains.

        Every connector/terminal clasp must be a real, fitted-by-shop clasp.
        Sealed/holder-dependent clasps are rejected here so future piece pickup
        never requires another owner to appear.
        """
        with self.lock:
            due = _as_date(due_date)
            if not groups:
                raise ValidationError("groups 至少包含一个家庭/持有人链段")
            connector_ids = list(connector_clasp_ids or [])
            if len(groups) > 1 and len(connector_ids) != len(groups) - 1:
                raise ValidationError("多段款式必须为相邻短链提供 groups-1 个连接扣")
            if len(groups) == 1 and not terminal_clasp_id:
                raise ValidationError("单条独立短链成品必须选择一个实配端扣作为出口")
            oid = self._new_id("order", order_id)
            if oid in self.db["orders"]:
                raise ConflictError(f"订单已存在: {oid}")
            if customer_id:
                self._require("people", customer_id)

            segment_inputs: List[Dict[str, Any]] = []
            short_chains: List[Dict[str, Any]] = []
            all_piece_ids: List[str] = []
            for i, group in enumerate(groups):
                owner_id = group.get("ownerId")
                self._require("people", owner_id)
                pids = list(group.get("pieceIds") or [])
                if not pids:
                    raise ValidationError("每个独立短链至少串入一件饰品")
                for pid in pids:
                    piece = self._require("pieces", pid)
                    if piece.get("chainId"):
                        raise ConflictError(f"饰品已在链上，不能重复排珠: {pid}")
                    all_piece_ids.append(pid)
                sid = group.get("segmentId") or f"short_{i + 1}"
                segment_inputs.append(
                    {
                        "id": sid,
                        "label": group.get("label") or f"{owner_id} 的独立短链",
                        "track": [{"type": "bead", "pieceId": pid} for pid in pids],
                        "left": {"type": "closed"},
                        "right": {"type": "closed"},
                    }
                )
                short_chains.append(
                    {
                        "shortChainId": group.get("shortChainId") or sid,
                        "ownerId": owner_id,
                        "segmentId": sid,
                        "pieceIds": pids,
                    }
                )

            selected_clasp_ids = connector_ids + ([terminal_clasp_id] if terminal_clasp_id else [])
            for cid in selected_clasp_ids:
                clasp = self._require("clasps", cid)
                if clasp.get("fitted"):
                    raise ConflictError(f"扣 {cid} 已被配用，不能重复选择")
                if not clasp.get("shopControlled"):
                    raise ValidationError("分段款必须选用店控扣，才能保证取珠不用其他持有人到场")

            chain_view = self.create_chain(
                segment_inputs,
                chain_id=chain_id,
                label=label or f"segmented-{oid}",
                style="segmented_short_chains",
            )
            real_chain_id = chain_view["id"]

            for i, cid in enumerate(connector_ids):
                self.install_connecting_clasp(
                    real_chain_id, cid, segment_inputs[i]["id"], segment_inputs[i + 1]["id"]
                )
            if terminal_clasp_id:
                self.install_clasp(
                    real_chain_id, terminal_clasp_id, segment_inputs[-1]["id"], "right"
                )

            order = {
                "id": oid,
                "kind": "finished_product_order",
                "businessModel": "segmented_short_chains",
                "status": "finished",
                "dueDate": due,
                "createdAt": _now(),
                "finishedAt": _now(),
                "customerId": customer_id,
                "chainId": real_chain_id,
                "pieceIds": all_piece_ids,
                "shortChains": short_chains,
                "connectorClaspIds": connector_ids,
                "terminalClaspId": terminal_clasp_id,
                "installedClaspIds": selected_clasp_ids,
                "pickupSchedule": list(pickup_schedule or []),
                "guarantee": "各持有人的珠在自己的独立短链上；所有连接/出口均为店控实配扣，取约定珠无需其他持有人到场。",
            }
            self.db["orders"][oid] = order
            self._event("segmented_order_finished", orderId=oid, chainId=real_chain_id, dueDate=due)
            self.commit()
            return self.get_order(oid)

    def get_order(self, order_id: str) -> Dict[str, Any]:
        with self.lock:
            return self._copy(self._require("orders", order_id))

    # ---------- actual individual removal / return ----------

    def remove_and_return_piece(
        self,
        chain_id: str,
        piece_id: str,
        return_to_id: str,
        on_date: Any,
        removal_id: Optional[str] = None,
        consent_id: Optional[str] = None,
        consent_ids: Optional[Sequence[str]] = None,
        note: Optional[str] = None,
    ) -> Dict[str, Any]:
        with self.lock:
            day = _as_date(on_date)
            recipient = self._require("people", return_to_id)
            piece = self._require("pieces", piece_id)
            ids = list(consent_ids or [])
            if consent_id:
                ids.append(consent_id)
            plan = self.plan_removal(chain_id, piece_id, day, ids)
            if not plan["feasible"] or not plan.get("chosenRoute"):
                raise RemovalNotFeasible(plan)

            allowed_recipients = {piece["ownerId"], *piece.get("authorizedRecipientIds", [])}
            if return_to_id not in allowed_recipients:
                raise ConflictError(
                    "只能拆还给物主或其预先授权的收件人；整条退回不等于可将该珠交给任意人",
                    {"allowedRecipientIds": sorted(allowed_recipients)},
                )

            route = plan["chosenRoute"]
            rid = self._new_id("removal", removal_id)
            if rid in self.db["removals"]:
                raise ConflictError(f"拆还记录已存在: {rid}")
            chain = self._require("chains", chain_id)
            loc = self._locate_on_chain(chain, piece_id)
            assert loc is not None
            si, ti = loc
            segment = chain["segments"][si]

            steps: List[Dict[str, Any]] = []
            temp_records: List[Dict[str, Any]] = []
            for seq, pid in enumerate(route["clearPieceIds"], start=1):
                trid = self._new_id("temp")
                record = {
                    "id": trid,
                    "removalId": rid,
                    "pieceId": pid,
                    "sequenceFromPort": seq,
                    "action": "temporarily_removed_to_shop_tray",
                    "at": _now(),
                    "restored": False,
                    "restoredAt": None,
                }
                self.db["temp_removals"].append(record)
                temp_records.append(record)
                neighbor = self.db["pieces"][pid]
                neighbor["status"] = "temp_removed"
                neighbor["location"] = "shop_temp_tray"
                neighbor["inShop"] = True
                steps.append({"step": seq, "action": "temporarily_remove", "pieceId": pid, "recordId": trid})

            auth = route["authorization"]
            steps.append(
                {
                    "step": len(steps) + 1,
                    "action": "open_exit_clasp",
                    "claspId": route["exitClaspId"],
                    "authorizationCode": auth.get("code"),
                    "consentId": auth.get("consentId"),
                }
            )

            # The target slides out; all temporary neighbors remain recorded.
            removed_track_item = segment["track"].pop(ti)
            assert removed_track_item["pieceId"] == piece_id
            steps.append(
                {
                    "step": len(steps) + 1,
                    "action": "slide_target_out",
                    "pieceId": piece_id,
                    "direction": route["direction"],
                    "ringsCrossed": route["ringsCrossed"],
                }
            )

            rethread_order = list(reversed(route["clearPieceIds"]))
            for seq, pid in enumerate(rethread_order, start=1):
                record = next(r for r in temp_records if r["pieceId"] == pid)
                record["restored"] = True
                record["restoredAt"] = _now()
                neighbor = self.db["pieces"][pid]
                neighbor["status"] = "on_chain"
                neighbor["location"] = "shop"
                neighbor["inShop"] = True
                steps.append({"step": len(steps) + 1, "action": "rethread", "pieceId": pid, "order": seq})

            piece["chainId"] = None
            piece["status"] = "individually_returned"
            piece["inShop"] = False
            piece["location"] = "person"
            piece["holderId"] = return_to_id
            piece["activeRentalId"] = None
            piece["lastIndividualReturnId"] = rid
            piece["returnedAt"] = _now()

            removal = {
                "id": rid,
                "kind": "individual_piece_return",
                "wholeChainReturned": False,
                "chainId": chain_id,
                "pieceId": piece_id,
                "pieceName": piece["name"],
                "date": day,
                "direction": route["direction"],
                "exitClaspId": route["exitClaspId"],
                "consentId": auth.get("consentId"),
                "temporaryPieceIdsClearOrder": route["clearPieceIds"],
                "rethreadOrder": rethread_order,
                "temporaryRemovalIds": [r["id"] for r in temp_records],
                "returnToId": return_to_id,
                "returnToName": recipient["name"],
                "steps": steps,
                "note": note,
                "createdAt": _now(),
            }
            self.db["removals"][rid] = removal
            self._event(
                "piece_individually_returned",
                removalId=rid,
                chainId=chain_id,
                pieceId=piece_id,
                returnToId=return_to_id,
            )
            self.commit()
            return self.get_removal(rid)

    def get_removal(self, removal_id: str) -> Dict[str, Any]:
        with self.lock:
            return self._copy(self._require("removals", removal_id))

    # ---------- whole-chain return is intentionally a different record ----------

    def return_whole_chain(
        self, chain_id: str, return_to_id: str, on_date: Any, return_id: Optional[str] = None
    ) -> Dict[str, Any]:
        with self.lock:
            day = _as_date(on_date)
            recipient = self._require("people", return_to_id)
            chain = self._require("chains", chain_id)
            rid = self._new_id("chainreturn", return_id)
            if rid in self.db["chain_returns"]:
                raise ConflictError(f"整链退回记录已存在: {rid}")
            piece_ids = self.current_piece_ids(chain_id)
            record = {
                "id": rid,
                "kind": "whole_chain_return",
                "treatedAsIndividualPieceReturn": False,
                "chainId": chain_id,
                "date": day,
                "returnToId": return_to_id,
                "returnToName": recipient["name"],
                "pieceIds": piece_ids,
                "createdAt": _now(),
            }
            self.db["chain_returns"][rid] = record
            chain["holderId"] = return_to_id
            chain["inShop"] = False
            chain["location"] = "person"
            for pid in piece_ids:
                piece = self.db["pieces"][pid]
                piece["holderId"] = return_to_id
                piece["inShop"] = False
                piece["location"] = "person"
            self._event("whole_chain_returned", returnId=rid, chainId=chain_id)
            self.commit()
            return self._copy(record)

    def receive_chain(self, chain_id: str, at: Optional[str] = None) -> Dict[str, Any]:
        """Bring a whole chain back to the counter after a person/rental custody."""
        with self.lock:
            chain = self._require("chains", chain_id)
            chain["holderId"] = None
            chain["inShop"] = True
            chain["location"] = "shop"
            for pid in self.current_piece_ids(chain_id):
                piece = self.db["pieces"][pid]
                piece["holderId"] = None
                piece["inShop"] = True
                piece["location"] = "shop"
            self._event("chain_received", chainId=chain_id, at=at or _now())
            self.commit()
            return self.get_chain_view(chain_id)

    # ---------- rentals ----------

    def create_rental(
        self,
        renter_id: str,
        start_date: Any,
        due_date: Any,
        rental_id: Optional[str] = None,
        chain_id: Optional[str] = None,
        piece_ids: Optional[Sequence[str]] = None,
        lender_id: Optional[str] = None,
        consigned: bool = False,
        note: Optional[str] = None,
    ) -> Dict[str, Any]:
        with self.lock:
            start = _as_date(start_date)
            due = _as_date(due_date)
            if due < start:
                raise ValidationError("due_date 不能早于 start_date")
            renter = self._require("people", renter_id)
            if lender_id:
                self._require("people", lender_id)
            ids: List[str] = list(piece_ids or [])
            if chain_id:
                chain = self._require("chains", chain_id)
                chain_pieces = self.current_piece_ids(chain_id)
                if ids:
                    missing = sorted(set(ids) - set(chain_pieces))
                    if missing:
                        raise ValidationError(f"指定饰品不在余链上: {missing}")
                else:
                    ids = chain_pieces
            if not ids:
                raise ValidationError("租借必须指定 chain_id 或至少一件 piece_id")
            for pid in ids:
                piece = self._require("pieces", pid)
                if piece.get("activeRentalId"):
                    raise ConflictError(f"饰品已有有效租借: {pid}")

            rid = self._new_id("rental", rental_id)
            if rid in self.db["rentals"]:
                raise ConflictError(f"租借编号已存在: {rid}")
            rental = {
                "id": rid,
                "status": "booked",
                "renterId": renter["id"],
                "renterName": renter["name"],
                "lenderId": lender_id,
                "chainId": chain_id,
                "pieceIds": ids,
                "startDate": start,
                "dueDate": due,
                "consigned": bool(consigned),
                "note": note,
                "createdAt": _now(),
                "handedOverAt": None,
                "returnedAt": None,
            }
            self.db["rentals"][rid] = rental
            self._event("rental_created", rentalId=rid, renterId=renter_id, pieceIds=ids)
            self.commit()
            return self._copy(rental)

    def hand_over_rental(self, rental_id: str) -> Dict[str, Any]:
        with self.lock:
            rental = self._require("rentals", rental_id)
            if rental["status"] not in {"booked"}:
                raise ConflictError(f"租借单状态不能交付: {rental['status']}")
            rental["status"] = "active"
            rental["handedOverAt"] = _now()
            for pid in rental["pieceIds"]:
                piece = self._require("pieces", pid)
                piece["activeRentalId"] = rental_id
                piece["holderId"] = rental["renterId"]
                piece["inShop"] = False
                piece["location"] = "rental"
            if rental.get("chainId"):
                chain = self._require("chains", rental["chainId"])
                chain["holderId"] = rental["renterId"]
                chain["inShop"] = False
                chain["location"] = "rental"
            self._event("rental_handed_over", rentalId=rental_id, renterId=rental["renterId"])
            self.commit()
            return self._copy(rental)

    def return_rental(self, rental_id: str) -> Dict[str, Any]:
        with self.lock:
            rental = self._require("rentals", rental_id)
            if rental["status"] != "active":
                raise ConflictError(f"只有有效租借可以收回: {rental['status']}")
            rental["status"] = "returned"
            rental["returnedAt"] = _now()
            for pid in rental["pieceIds"]:
                piece = self._require("pieces", pid)
                piece["activeRentalId"] = None
                piece["holderId"] = None
                piece["inShop"] = True
                piece["location"] = "shop"
            if rental.get("chainId"):
                chain = self._require("chains", rental["chainId"])
                chain["holderId"] = None
                chain["inShop"] = True
                chain["location"] = "shop"
            self._event("rental_returned", rentalId=rental_id)
            self.commit()
            return self._copy(rental)

    # ---------- restart-friendly queries ----------

    def locate_piece(self, piece_id: str) -> Dict[str, Any]:
        with self.lock:
            piece = self._require("pieces", piece_id)
            holder = self.db["people"].get(piece.get("holderId")) if piece.get("holderId") else None
            owner = self.db["people"].get(piece.get("ownerId"))
            removal = None
            if piece.get("lastIndividualReturnId"):
                removal = self.db["removals"].get(piece["lastIndividualReturnId"])
            return {
                "pieceId": piece_id,
                "name": piece.get("name"),
                "status": piece.get("status"),
                "inShop": piece.get("inShop"),
                "location": piece.get("location"),
                "ownerId": piece.get("ownerId"),
                "ownerName": owner.get("name") if owner else None,
                "holderId": piece.get("holderId"),
                "holderName": holder.get("name") if holder else ("柜台" if piece.get("inShop") else None),
                "chainId": piece.get("chainId"),
                "activeRentalId": piece.get("activeRentalId"),
                "consignedForRental": piece.get("consignedForRental"),
                "individualReturn": bool(piece.get("lastIndividualReturnId")),
                "individualRemovalId": piece.get("lastIndividualReturnId"),
                "returnedTo": removal.get("returnToId") if removal else None,
                "returnedToName": removal.get("returnToName") if removal else None,
            }

    def remaining_chain_report(self, chain_id: str) -> Dict[str, Any]:
        with self.lock:
            view = self.get_chain_view(chain_id)
            holder = self.db["people"].get(view.get("holderId")) if view.get("holderId") else None
            consigned = [
                pid for pid in view["currentPieceIds"] if self.db["pieces"].get(pid, {}).get("consignedForRental")
            ]
            active_rental = next(
                (
                    r
                    for r in self.db["rentals"].values()
                    if r.get("chainId") == chain_id and r.get("status") == "active"
                ),
                None,
            )
            return {
                "chainId": chain_id,
                "pieceIds": view["currentPieceIds"],
                "consignedPieceIds": consigned,
                "activeRentalId": active_rental.get("id") if active_rental else None,
                "attachedClaspIds": view["attachedClaspIds"],
                "claspConnections": view["claspConnections"],
                "inShop": view.get("inShop"),
                "location": view.get("location"),
                "holderId": view.get("holderId"),
                "holderName": holder.get("name") if holder else ("柜台" if view.get("inShop") else None),
            }
