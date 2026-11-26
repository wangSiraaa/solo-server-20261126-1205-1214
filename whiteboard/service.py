"""应用服务层：把领域算法接到持久化状态上。"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Dict, List, Optional, Sequence

from .model import (
    MAX_DRAFTS_PER_SESSION,
    MAX_STEPS_PER_DRAFT,
    NotFoundError as ModelNotFound,
    WhiteboardError,
    best_replay_permutation,
    clone_board,
    diff_boards,
    make_board,
    normalize_convention,
    order_is_allowed,
    plan_merge,
    simulate,
)
from .store import Store, new_id, now

SAFE_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")


class Service:
    def __init__(self, store: Store):
        self.store = store

    # ------------------------------------------------------------------
    # Teacher / class
    # ------------------------------------------------------------------
    def create_teacher_session(self, payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        payload = payload or {}
        with self.store.lock:
            teacher_id = payload.get("id") or new_id("teacher")
            teacher = {
                "id": teacher_id,
                "name": payload.get("name", "未命名老师"),
                "created_at": now(),
                "class_ids": [],
            }
            self.store.put("teacher_sessions", teacher_id, teacher)
            self.store.save()
            return teacher

    def create_class(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        payload = payload or {}
        with self.store.lock:
            convention = normalize_convention(payload.get("convention", "patch"))
            rows = int(payload.get("rows", payload.get("row_count", 1)))
            cols = int(payload.get("cols", payload.get("col_count", 3)))
            board = make_board(
                rows,
                cols,
                base_color=payload.get("base_color", "white"),
                walls=payload.get("walls"),
            )
            board_id = new_id("board")
            board["board_id"] = board_id
            board["revision"] = 1
            board["lineage"] = []
            self.store.put("boards", board_id, board)

            session_id = payload.get("id") or new_id("class")
            if not SAFE_ID_RE.match(str(session_id)):
                raise WhiteboardError("id 只能包含字母数字、_ . : -，长度不超过64")
            if session_id in self.store.state["sessions"]:
                raise WhiteboardError(f"课节 id 已存在: {session_id}")

            teacher_id = payload.get("teacher_id")
            if teacher_id and teacher_id in self.store.state["teacher_sessions"]:
                self.store.state["teacher_sessions"][teacher_id]["class_ids"].append(session_id)

            lesson = self.normalize_lesson(payload.get("lesson") or {})
            for chinese_key, english_key in (
                ("必留格", "must_keep"),
                ("禁涂格", "forbidden"),
                ("禁涂", "forbidden"),
                ("allowed_orders", "allowed_orders"),
                ("笔法顺序", "allowed_orders"),
                ("顺序", "allowed_orders"),
                ("precedence", "precedence"),
                ("先后", "precedence"),
            ):
                if chinese_key in payload and english_key not in payload:
                    payload[english_key] = payload[chinese_key]
            for key in ("must_keep", "forbidden", "forbidden_paint"):
                if key in payload:
                    lesson[key] = payload[key]
            lesson = self.normalize_lesson(lesson)
            session = {
                "id": session_id,
                "title": payload.get("title", "未命名课堂"),
                "teacher_id": teacher_id,
                "convention": convention,
                "created_at": now(),
                "current_board_id": board_id,
                "published_board_ids": [board_id],
                "group_ids": [],
                "draft_ids": [],
                "merge_ids": [],
                "lesson": lesson,
                "allowed_orders": payload.get("allowed_orders") or lesson.get("allowed_orders") or [],
                "precedence": payload.get("precedence") or lesson.get("precedence") or [],
                "scores": payload.get("scores") or {},
                "constraints_note": {
                    "must_keep": "必留格：发表总板上必须保持指定/教案底色",
                    "forbidden": "禁涂格：最终不得出现禁色，重演时画笔也不得把该格涂成禁色",
                },
            }
            self.store.put("sessions", session_id, session)
            self.store.save()
            return self.get_class(session_id)

    def get_class(self, session_id: str) -> Dict[str, Any]:
        with self.store.lock:
            session = self.store.get("sessions", session_id)
            result = dict(session)
            result["current_board"] = self.store.state["boards"][session["current_board_id"]]
            # 不主动展开各小组私有 board，避免课节列表泄露未发表内容。
            result["groups"] = [
                {
                    "id": gid,
                    "name": self.store.state["groups"][gid]["name"]
                    if "groups" in self.store.state and gid in self.store.state["groups"]
                    else None,
                }
                for gid in session.get("group_ids", [])
            ] if "groups" in self.store.state else []
            result["drafts_summary"] = [
                self._draft_summary(self.store.state["drafts"][did])
                for did in session.get("draft_ids", [])
                if "drafts" in self.store.state and did in self.store.state["drafts"]
            ]
            return result

    def list_sessions(self) -> List[Dict[str, Any]]:
        with self.store.lock:
            return [
                {
                    "id": s["id"],
                    "title": s.get("title"),
                    "convention": s.get("convention"),
                    "current_board_id": s.get("current_board_id"),
                    "draft_count": len(s.get("draft_ids", [])),
                    "merge_count": len(s.get("merge_ids", [])),
                }
                for s in self.store.state["sessions"].values()
            ]

    def update_lesson(self, session_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        with self.store.lock:
            session = self.store.get("sessions", session_id)
            payload = dict(payload or {})
            for cn, en in (
                ("必留格", "must_keep"),
                ("禁涂格", "forbidden"),
                ("禁涂", "forbidden"),
                ("笔法顺序", "allowed_orders"),
                ("顺序", "allowed_orders"),
                ("先后", "precedence"),
            ):
                if cn in payload and en not in payload:
                    payload[en] = payload[cn]
            if "lesson" in payload:
                lesson = self.normalize_lesson(payload["lesson"] or {})
            else:
                lesson = dict(session.get("lesson") or {})
                for key in ("must_keep", "forbidden", "forbidden_paint"):
                    if key in payload:
                        lesson[key] = payload[key]
                lesson = self.normalize_lesson(lesson)
            if "allowed_orders" in payload:
                session["allowed_orders"] = payload["allowed_orders"]
            if "precedence" in payload:
                session["precedence"] = payload["precedence"]
            if "scores" in payload:
                session["scores"] = payload["scores"]
            session["lesson"] = lesson
            self.store.save()
            return session["lesson"]

    @staticmethod
    def normalize_lesson(lesson: Dict[str, Any]) -> Dict[str, Any]:
        lesson = dict(lesson or {})
        aliases = {
            "必留格": "must_keep",
            "reserved_cells": "must_keep",
            "keep": "must_keep",
            "禁涂格": "forbidden",
            "禁涂": "forbidden",
            "forbidden_paint": "forbidden",
            "banned": "forbidden",
        }
        for old, new in aliases.items():
            if old in lesson and new not in lesson:
                lesson[new] = lesson[old]

        def looks_like_cell(item: Any) -> bool:
            return isinstance(item, str) or (
                isinstance(item, (list, tuple)) and len(item) == 2 and not isinstance(item[0], dict)
            )

        keep = lesson.get("must_keep")
        if isinstance(keep, list) and keep and all(looks_like_cell(x) for x in keep):
            lesson["must_keep"] = [{"cell": x} for x in keep]

        forbidden = lesson.get("forbidden")
        if isinstance(forbidden, list) and forbidden and all(looks_like_cell(x) for x in forbidden):
            banned_color = lesson.get("forbidden_color", lesson.get("banned_color", "*"))
            lesson["forbidden"] = [{"cell": x, "color": banned_color} for x in forbidden]
        return lesson

    # ------------------------------------------------------------------
    # Groups / private previews
    # ------------------------------------------------------------------
    def create_group(self, session_id: str, payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        payload = payload or {}
        with self.store.lock:
            session = self.store.get("sessions", session_id)
            master = self.store.state["boards"][session["current_board_id"]]
            group_id = payload.get("id") or new_id("group")
            if not SAFE_ID_RE.match(str(group_id)):
                raise WhiteboardError("id 只能包含字母数字、_ . : -，长度不超过64")
            groups = self.store.state.setdefault("groups", {})
            if group_id in groups:
                raise WhiteboardError(f"小组 id 已存在: {group_id}")
            opened_board = clone_board(master)
            group = {
                "id": group_id,
                "session_id": session_id,
                "name": payload.get("name", group_id),
                "created_at": now(),
                "base_board_id": session["current_board_id"],
                "opened_board": opened_board,
                "board": clone_board(opened_board),
                "step_log": [],
                "draft_ids": [],
            }
            groups[group_id] = group
            session["group_ids"].append(group_id)
            self.store.save()
            return group

    def get_group(self, group_id: str) -> Dict[str, Any]:
        with self.store.lock:
            return self.store.get("groups", group_id)

    def preview_group(self, group_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        with self.store.lock:
            group = self.store.get("groups", group_id)
            steps = self._steps_from_payload(payload)
            result = simulate(group["board"], steps)
            return {
                "group_id": group_id,
                "saved_board": group["board"],
                "preview_board": result["board"],
                "trace": result["trace"],
                "changes": diff_boards(group["board"], result["board"]),
                "step_count": len(steps),
                "draft_step_limit": MAX_STEPS_PER_DRAFT,
            }

    def apply_group_operations(self, group_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        with self.store.lock:
            group = self.store.get("groups", group_id)
            steps = self._steps_from_payload(payload)
            result = simulate(group["board"], steps)
            group["board"] = result["board"]
            group["step_log"].extend(steps)
            self.store.save()
            return {
                "group_id": group_id,
                "board": group["board"],
                "trace": result["trace"],
                "changes": diff_boards(group["opened_board"], group["board"]),
                "step_log_count": len(group["step_log"]),
            }

    # ------------------------------------------------------------------
    # Drafts
    # ------------------------------------------------------------------
    def create_draft(self, session_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        payload = payload or {}
        with self.store.lock:
            session = self.store.get("sessions", session_id)
            if len(session.get("draft_ids", [])) >= MAX_DRAFTS_PER_SESSION:
                raise WhiteboardError(f"单节至多 {MAX_DRAFTS_PER_SESSION} 稿")

            group = None
            group_id = payload.get("group_id")
            if group_id:
                group = self.store.get("groups", group_id)
                if group["session_id"] != session_id:
                    raise WhiteboardError("小组不属于该课节")

            use_applied = payload.get("use_applied")
            explicit_patch = payload.get("patch", payload.get("changes"))
            native_trace: List[Dict[str, Any]] = []
            has_step_request = any(
                key in payload for key in ("steps", "operations", "ops")
            ) or any(key in payload for key in ("op", "type", "action"))

            if has_step_request:
                steps = self._steps_from_payload(payload)
                if group is not None:
                    observed_board = clone_board(group["board"])
                else:
                    observed_board = clone_board(
                        payload.get("observed_board")
                        or self.store.state["boards"][session["current_board_id"]]
                    )
                if len(steps) > MAX_STEPS_PER_DRAFT:
                    raise WhiteboardError(
                        f"每稿至多 {MAX_STEPS_PER_DRAFT} 步，当前 {len(steps)} 步"
                    )
                simulated = simulate(observed_board, steps)
                preview_board = simulated["board"]
                native_trace = simulated["trace"]
                patch = diff_boards(observed_board, preview_board)
            elif explicit_patch is not None or payload.get("preview_board") is not None:
                # 允许老师直接给“照样贴图”稿件：显式 patch，或 observed/preview 两块板。
                steps = self._steps_from_payload({"steps": payload.get("steps", [])})
                observed_board = clone_board(
                    payload.get("observed_board")
                    or (group["board"] if group is not None else None)
                    or self.store.state["boards"][session["current_board_id"]]
                )
                preview_board = clone_board(payload.get("preview_board") or observed_board)
                if explicit_patch is not None:
                    patch = self._normalize_patch(explicit_patch)
                    from .model import apply_patch

                    apply_patch(preview_board, patch)
                else:
                    patch = diff_boards(observed_board, preview_board)
                native_trace = list(payload.get("native_trace") or [])
            elif group is not None and use_applied is not False:
                # 学生先通过 /operations 改自己的板，再空请求交稿时，
                # 默认把该小组自开板以来的操作段交给老师。
                steps = list(group.get("step_log") or [])
                observed_board = clone_board(group["opened_board"])
                if len(steps) > MAX_STEPS_PER_DRAFT:
                    raise WhiteboardError(
                        f"每稿至多 {MAX_STEPS_PER_DRAFT} 步，当前 {len(steps)} 步"
                    )
                simulated = simulate(observed_board, steps)
                preview_board = simulated["board"]
                native_trace = simulated["trace"]
                patch = diff_boards(observed_board, preview_board)
            else:
                raise WhiteboardError(
                    "稿件必须包含 steps、patch/preview_board，或对小组已应用操作调用 /groups/{id}/drafts"
                )

            promise = self._build_promise(observed_board)

            draft_id = payload.get("id") or new_id("draft")
            if not SAFE_ID_RE.match(str(draft_id)):
                raise WhiteboardError("id 只能包含字母数字、_ . : -，长度不超过64")
            drafts = self.store.state.setdefault("drafts", {})
            if draft_id in drafts:
                raise WhiteboardError(f"稿件 id 已存在: {draft_id}")

            draft = {
                "id": draft_id,
                "session_id": session_id,
                "group_id": group_id,
                "title": payload.get("title", payload.get("name", draft_id)),
                "author": payload.get("author", payload.get("group_name")),
                "score": payload.get("score", payload.get("points", payload.get("分值", 1))),
                "steps": steps,
                "observed_board": observed_board,
                "promise": promise,
                "preview_board": preview_board,
                "native_trace": native_trace,
                "patch": patch,
                "status": "available",
                "dispositions": [],
                "created_at": now(),
            }
            drafts[draft_id] = draft
            session["draft_ids"].append(draft_id)
            if group is not None:
                group["draft_ids"].append(draft_id)
                if payload.get("commit_to_group", True) and has_step_request:
                    group["board"] = clone_board(preview_board)
                    group["step_log"].extend(steps)
            self.store.save()
            return draft

    def get_draft(self, draft_id: str) -> Dict[str, Any]:
        with self.store.lock:
            return self.store.get("drafts", draft_id)

    def update_draft(self, draft_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        with self.store.lock:
            draft = self.store.get("drafts", draft_id)
            for key in ("score", "title", "status"):
                if key in payload:
                    draft[key] = payload[key]
            self.store.save()
            return draft

    def list_drafts(self, session_id: str) -> List[Dict[str, Any]]:
        with self.store.lock:
            session = self.store.get("sessions", session_id)
            return [self.store.state["drafts"][d] for d in session["draft_ids"]]

    # ------------------------------------------------------------------
    # Planning / publishing
    # ------------------------------------------------------------------
    def plan_merge(self, session_id: str, payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        payload = payload or {}
        with self.store.lock:
            session = self.store.get("sessions", session_id)
            master = self.store.state["boards"][session["current_board_id"]]
            drafts, candidate_ids = self._candidate_drafts(session, payload)
            lesson = self.normalize_lesson(payload.get("lesson") or session.get("lesson") or {})
            allowed_orders = self._allowed_orders(session, payload)
            precedence = self._precedence(session, payload)
            requested_order = payload.get("order")
            return plan_merge(
                session.get("convention", "patch"),
                master,
                drafts,
                candidate_ids,
                lesson=lesson,
                allowed_orders=allowed_orders,
                precedence=precedence,
                requested_order=requested_order,
            )

    def publish_merge(self, session_id: str, payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        payload = payload or {}
        with self.store.lock:
            session = self.store.get("sessions", session_id)
            master = self.store.state["boards"][session["current_board_id"]]
            drafts, candidate_ids = self._candidate_drafts(session, payload)
            lesson = self.normalize_lesson(payload.get("lesson") or session.get("lesson") or {})
            allowed_orders = self._allowed_orders(session, payload)
            precedence = self._precedence(session, payload)

            if payload.get("draft_ids") or payload.get("order"):
                selected = list(payload.get("draft_ids") or payload.get("order") or [])
                order = payload.get("order")
                if session["convention"] == "patch":
                    from .model import evaluate_patch

                    best = evaluate_patch(master, drafts, selected, lesson)
                else:
                    if order is not None:
                        from .model import evaluate_replay_order

                        best = evaluate_replay_order(master, drafts, order, lesson)
                        if not order_is_allowed(order, allowed_orders, precedence):
                            best["legal"] = False
                            best.setdefault("violations", []).append(
                                {
                                    "kind": "order_not_allowed",
                                    "message": "发表顺序不在老师允许的笔法先后中",
                                }
                            )
                    else:
                        exact = best_replay_permutation(
                            master,
                            drafts,
                            selected,
                            lesson=lesson,
                            allowed_orders=allowed_orders,
                            precedence=precedence,
                        )
                        best = exact["best"]
                        if best is None:
                            attempts = exact.get("attempts") or []
                            violations = []
                            for attempt in attempts:
                                violations.extend(attempt.get("violations") or [])
                            best = {
                                "legal": False,
                                "violations": violations
                                or [{"message": "给定稿件集合没有允许且合法的重演顺序"}],
                            }
            else:
                planned = plan_merge(
                    session["convention"],
                    master,
                    drafts,
                    candidate_ids,
                    lesson=lesson,
                    allowed_orders=allowed_orders,
                    precedence=precedence,
                )
                best = planned["best"]

            if not best or not best.get("legal"):
                reasons = best.get("violations") if best else ["没有任何合法方案"]
                raise WhiteboardError(f"没有可合法发表的合班: {reasons}")

            board = best["board"]
            new_board_id = payload.get("new_board_id") or new_id("board")
            board["board_id"] = new_board_id
            board["revision"] = len(session.get("published_board_ids", [])) + 1
            board["parent_board_id"] = session["current_board_id"]
            board["merge_id"] = new_id("merge")
            board["signed_at"] = now()
            self.store.put("boards", new_board_id, board)

            merge_id = board["merge_id"]
            selected_ids = best["draft_ids"]
            dispositions = []
            for draft_id in candidate_ids:
                if draft_id in selected_ids:
                    decision = "accepted"
                    drafts[draft_id]["status"] = "published"
                    drafts[draft_id]["published_board_id"] = new_board_id
                    drafts[draft_id]["merge_id"] = merge_id
                else:
                    decision = "not_selected"
                item = {
                    "merge_id": merge_id,
                    "draft_id": draft_id,
                    "decision": decision,
                    "at": now(),
                }
                dispositions.append(item)
                drafts[draft_id].setdefault("dispositions", []).append(item)

            result_without_board = {k: v for k, v in best.items() if k != "board"}
            merge = {
                "id": merge_id,
                "session_id": session_id,
                "convention": session["convention"],
                "created_at": now(),
                "parent_board_id": session["current_board_id"],
                "published_board_id": new_board_id,
                "score": best["score"],
                "selected_draft_ids": selected_ids,
                "order": best.get("order", selected_ids),
                "result": result_without_board,
                "dispositions": dispositions,
                "request_payload": {
                    k: v for k, v in payload.items() if k not in {"lesson"}
                },
            }
            self.store.put("merges", merge_id, merge)
            session["merge_ids"].append(merge_id)
            session["current_board_id"] = new_board_id
            session["published_board_ids"].append(new_board_id)
            self.store.save()
            return {
                "merge": merge,
                "published_board": board,
                "published_board_id": new_board_id,
                "current_board_id": new_board_id,
                "dispositions": dispositions,
            }

    def get_board(self, board_id: str) -> Dict[str, Any]:
        with self.store.lock:
            return self.store.get("boards", board_id)

    def get_merge(self, merge_id: str) -> Dict[str, Any]:
        with self.store.lock:
            return self.store.get("merges", merge_id)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _steps_from_payload(self, payload: Any) -> List[Dict[str, Any]]:
        if isinstance(payload, list):
            steps = payload
        elif isinstance(payload, dict):
            steps = payload.get("steps", payload.get("operations", payload.get("ops")))
            if steps is None and ("op" in payload or "type" in payload or "action" in payload):
                steps = [payload]
        else:
            steps = None
        if not isinstance(steps, list):
            raise WhiteboardError("请求需要 steps 数组或单个操作对象")
        return steps

    def _candidate_drafts(
        self, session: Dict[str, Any], payload: Dict[str, Any]
    ) -> tuple[Dict[str, Dict[str, Any]], List[str]]:
        all_drafts = self.store.state.setdefault("drafts", {})
        if payload.get("draft_ids") is not None:
            ids = list(dict.fromkeys(payload["draft_ids"]))
        else:
            ids = list(session.get("draft_ids", []))
            if not payload.get("include_published", False):
                ids = [d for d in ids if all_drafts.get(d, {}).get("status") != "published"]
        drafts = {d: all_drafts[d] for d in ids if d in all_drafts}
        missing = [d for d in ids if d not in all_drafts]
        if missing:
            raise KeyError(f"draft not found: {', '.join(missing)}")

        # 老师可在教案或请求中用 {draft_id: score} 统一给每份演示分值。
        score_map: Dict[str, Any] = {}
        score_map.update(session.get("scores") or {})
        score_map.update((session.get("lesson") or {}).get("scores") or {})
        score_map.update(payload.get("scores") or {})
        for draft_id, score in score_map.items():
            if draft_id in all_drafts:
                all_drafts[draft_id]["score"] = score
        return drafts, ids

    def _allowed_orders(self, session: Dict[str, Any], payload: Dict[str, Any]) -> List[List[str]]:
        if payload.get("allowed_orders") is not None:
            return payload["allowed_orders"]
        if payload.get("order_rules") is not None:
            return payload["order_rules"]
        return session.get("allowed_orders") or []

    def _precedence(self, session: Dict[str, Any], payload: Dict[str, Any]) -> List[List[str]]:
        if payload.get("precedence") is not None:
            return payload["precedence"]
        if payload.get("order_precedence") is not None:
            return payload["order_precedence"]
        return session.get("precedence") or []

    def _normalize_patch(self, raw: Dict[str, Any]) -> Dict[str, Any]:
        if not isinstance(raw, dict):
            raise WhiteboardError("patch/changes 必须是对象")
        if "cells" in raw or "walls" in raw:
            return {"cells": raw.get("cells") or {}, "walls": raw.get("walls") or {}}
        return {
            "cells": raw.get("cell_changes") or raw.get("cell") or {},
            "walls": raw.get("wall_changes") or raw.get("edge_changes") or raw.get("walls") or {},
        }

    def _build_promise(self, board: Dict[str, Any]) -> Dict[str, Any]:
        """学生交稿时承诺亲眼看到的全部格色与共边挡线。"""
        cells = []
        for r in range(board["rows"]):
            for c in range(board["cols"]):
                cells.append({"cell": [r, c], "color": board["cells"][r][c]})
        edges = [
            {"edge": edge, "wall": bool(has_wall)}
            for edge, has_wall in sorted(board.get("walls", {}).items())
        ]
        canonical = json.dumps(
            {"cells": cells, "edges": edges}, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]
        return {
            "stage": "submission",
            "description": "交稿时小组板上的格色与挡线；贴图约定据此解释原稿为何扩散到该范围",
            "cells": cells,
            "edges": edges,
            "digest": digest,
        }

    def _draft_summary(self, draft: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "id": draft["id"],
            "title": draft.get("title"),
            "group_id": draft.get("group_id"),
            "score": draft.get("score"),
            "status": draft.get("status"),
            "steps": draft.get("steps"),
        }
