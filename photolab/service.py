"""业务用例层：业务端开单 → 备料选片 → 洗印单 → 师傅确认曝光 → 接样 → 取件。

状态机：
  批次 intake → prepared（遮片已刻、已登记）→ open（洗印单已下）→ closed（全部取件）
  订单 intake → confirmed → printing → ready → delivered
  纸   exposed（已局部加光的试样）→ printing → printed → delivered
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from .domain import (
    StepCard, StepLevel, cell_key, parse_cell,
    ConflictError, NotFoundError, ValidationError,
)
from .planner import plan_batch
from .render import render_paper
from .store import Store, dumps, loads

DARKROOM = "fixed-mask"


class PhotoLabService:
    def __init__(self, db_path: str | Path, product_dir: str | Path):
        self.store = Store(db_path)
        self.product_dir = Path(product_dir)

    def close(self) -> None:
        self.store.close()

    # ---------- 业务端开单 ----------
    def intake_batch(self, batch_id: str, rows: int, cols: int,
                     card_pairs: list[list], sheet_budget: int, shot_budget: int,
                     papers: list[dict], order: dict) -> dict:
        """一次性接件：批次、已曝光纸、顾客圈选与旧试印沿用数。"""
        if rows <= 0 or cols <= 0:
            raise ValidationError("格子行列数必须为正")
        if sheet_budget < 0 or shot_budget < 0:
            raise ValidationError("片材/拍数预算不能为负")
        card = StepCard.from_pairs(card_pairs)
        self.store.create_batch(
            batch_id, DARKROOM, rows, cols, card.as_dict(), sheet_budget, shot_budget
        )
        seen_papers: set[str] = set()
        for paper in papers:
            pid = paper["id"]
            if pid in seen_papers:
                raise ValidationError(f"纸号 {pid} 重复")
            seen_papers.add(pid)
            start = {cell_key(parse_cell(k)): int(v) for k, v in paper.get("start", {}).items()}
            beam = {cell_key(parse_cell(k)): int(v)
                    for k, v in paper.get("beam_per_cell", {}).items()}
            self._validate_grid_keys(start, rows, cols)
            self._validate_grid_keys(beam, rows, cols)
            if any(v < 0 for v in start.values()):
                raise ValidationError("已吃进光量不能为负")
            if any(v <= 0 for v in beam.values()):
                raise ValidationError("每拍透光量必须为正整数")
            self.store.add_paper(pid, batch_id, start, beam,
                                 reused=paper.get("reused", False))

        reqs = []
        for req in order.get("requirements", []):
            if req["paper_id"] not in seen_papers:
                raise ValidationError(f"圈选引用了不存在的纸 {req['paper_id']}")
            parse_cell(req["cell"])  # 校验坐标
            if req["role"] not in ("face", "background"):
                raise ValidationError("角色只能是 face（必须保住）或 background（可以加深）")
            card.level_by_name(req["level"])  # 校验色阶名称
            reqs.append({
                "paper_id": req["paper_id"], "cell": req["cell"],
                "role": req["role"], "level": req["level"],
            })
        reuse_count = int(order.get("reuse_count", 0))
        if reuse_count < 0 or reuse_count > len(papers):
            raise ValidationError("旧试印沿用数超出本批纸数")
        self.store.create_order(
            order["id"], batch_id, order.get("customer", "订户"), reuse_count, reqs
        )
        if reuse_count:
            self.store.hold_trials(order["id"], reuse_count)
        return {"batch_id": batch_id, "order_id": order["id"], "status": "intake"}

    @staticmethod
    def _validate_grid_keys(d: dict, rows: int, cols: int) -> None:
        for key in d:
            r, c = parse_cell(key)
            if not (0 <= r < rows and 0 <= c < cols):
                raise ValidationError(f"格子 {key} 超出 {rows}x{cols} 纸面")

    # ---------- 备料：开批前选片刻片 ----------
    def prepare(self, batch_id: str) -> dict:
        """用排程器验证整批可完成；可行则把遮片形状与可翻动位置登记备料。"""
        brow = self.store.get_batch(batch_id)
        self.store.freeze_masks(batch_id)  # 仅 intake 状态可登记
        card = StepCard([StepLevel(**lv) for lv in loads(brow["card_json"])])
        order = self._load_order(batch_id)
        reqs_by_paper = self._requirements_by_paper(order["id"])

        papers = []
        for prow in self.store.list_papers(batch_id):
            if prow["reused"]:
                # 沿用已付费旧试印：不再安排任何新曝光，也不占遮片/拍数预算
                continue
            papers.append({
                "id": prow["id"],
                "rows": brow["rows"], "cols": brow["cols"],
                "start_lights": {parse_cell(k): v for k, v in loads(prow["start_json"]).items()},
                "beam_per_cell": {parse_cell(k): v for k, v in loads(prow["beam_json"]).items()},
            })
        plan = plan_batch(
            papers, reqs_by_paper, card,
            brow["sheet_budget"], brow["shot_budget"],
        )
        if not plan.feasible:
            return {"feasible": False, "reason": plan.reason}

        # 登记所有要用的遮片形状，以及它们在这张纸上能翻动到的全部合规位置
        from .planner import placements_for
        for shape in plan.masks:
            for placement in placements_for(shape, brow["rows"], brow["cols"]):
                self.store.register_mask(
                    batch_id,
                    [list(c) for c in placement.shape],
                    list(placement.offset),
                    [list(c) for c in sorted(placement.openings)],
                )
        self.store.set_batch_status(batch_id, "prepared", plan.to_dict())
        return {"feasible": True, "plan": plan.to_dict(),
                "registered_masks": len(self.store.list_masks(batch_id))}

    # ---------- 下洗印单 ----------
    def open_batch(self, batch_id: str) -> dict:
        brow = self.store.get_batch(batch_id)
        if brow["status"] != "prepared":
            raise ConflictError(f"批次状态为 {brow['status']}，须先备料成功才能开洗印单")
        plan = loads(brow["plan_json"])
        order = self._load_order(batch_id)
        for pp in plan["papers"]:
            self.store.save_paper_plan(pp["paper_id"], pp)
        self.store.set_batch_status(batch_id, "open", plan)
        self.store.set_order_status(order["id"], "confirmed")
        # 沿用旧试印的纸无需进曝光流程，直接渲染库存成品占位
        for prow in self.store.list_papers(batch_id):
            if prow["reused"]:
                self._render_from_accumulated(prow, brow, suffix="旧试印沿用")
        return {"batch_id": batch_id, "status": "open", "print_sheet": plan}

    def print_sheet(self, batch_id: str) -> dict:
        """师傅手里的洗印单：每纸逐拍写清遮片形状、翻动位置、透光格与光量。"""
        brow = self.store.get_batch(batch_id)
        if brow["status"] not in ("open", "closed"):
            raise ConflictError("洗印单只在开批后可取")
        return {"batch_id": batch_id, "print_sheet": loads(brow["plan_json"])}

    # ---------- 师傅确认实际曝光 ----------
    def confirm_shot(self, paper_id: str, exposures: Optional[dict] = None,
                     note: str = "") -> dict:
        """一拍的曝光回条。服务端按洗印单核对，迟到/重复回条一律拒收。"""
        prow = self.store.get_paper(paper_id)
        brow = self.store.get_batch(prow["batch_id"])
        if brow["status"] not in ("open", "closed"):
            raise ConflictError("批次未开批，不能曝光")
        if prow["reused"]:
            raise ConflictError(f"纸 {paper_id} 沿用旧试印，不再曝光")

        paper_plan = loads(prow["plan_json"])
        seq = self.store.next_shot_seq(paper_id)
        if seq >= len(paper_plan["shots"]):
            raise ConflictError(
                f"纸 {paper_id} 洗印单的 {len(paper_plan['shots'])} 拍已全部落账，"
                "迟到回条不能把同一张纸当新纸再曝光"
            )
        if prow["status"] == "printed":
            raise ConflictError(f"纸 {paper_id} 已洗成成品，不能再当新纸曝光")

        expected = paper_plan["shots"][seq]

        # 遮片位置必须在备料登记表上，且正是洗印单本拍指定的那一拍
        self._assert_registered(prow["batch_id"], expected["mask"], expected["offset"])
        actual = dict(expected["exposures"]) if exposures is None else {
            cell_key(parse_cell(k)): int(v) for k, v in exposures.items()
        }
        if actual != expected["exposures"]:
            raise ConflictError(
                f"第 {seq} 拍回条光量 {actual} 与洗印单 {expected['exposures']} 不符，已拒收"
            )

        accumulated = loads(prow["accumulated_json"])
        for key, amount in actual.items():
            accumulated[key] = accumulated.get(key, 0) + amount  # 只增不减
        self.store.append_shot(
            paper_id, seq, expected["mask"], expected["offset"], actual, accumulated
        )

        done = seq + 1
        if done == len(paper_plan["shots"]):
            paths = self._render_from_accumulated(self.store.get_paper(paper_id), brow)
            return {"paper_id": paper_id, "seq": seq, "accumulated": accumulated,
                    "completed": True, "product": paths}
        return {"paper_id": paper_id, "seq": seq, "accumulated": accumulated,
                "completed": False}

    def _assert_registered(self, batch_id: str, shape: list, offset: list) -> None:
        for row in self.store.list_masks(batch_id):
            if loads(row["shape_json"]) == shape and loads(row["offset_json"]) == offset:
                return
        raise ConflictError(
            f"遮片 {shape} @ {offset} 不在备料登记表上，开工后不再补刻"
        )

    # ---------- 接回新样 ----------
    def receive_sample(self, sample_id: str, paper_id: str, note: str = "") -> dict:
        """显影后接样：以累计光量推算实际成色阶，登记样片。"""
        prow = self.store.get_paper(paper_id)
        brow = self.store.get_batch(prow["batch_id"])
        card = StepCard([StepLevel(**lv) for lv in loads(brow["card_json"])])
        accumulated = loads(prow["accumulated_json"])
        observed = {
            key: (lv.name if (lv := card.level_of(light)) else "未入阶")
            for key, light in sorted(accumulated.items())
        }
        self.store.add_sample(sample_id, paper_id, observed, note)
        return {"sample_id": sample_id, "paper_id": paper_id, "observed": observed}

    # ---------- 凭订单领取成品 ----------
    def deliver(self, order_id: str) -> dict:
        order = self.store.get_order(order_id)
        batch_id = order["batch_id"]
        brow = self.store.get_batch(batch_id)
        products = []
        for prow in self.store.list_papers(batch_id):
            if prow["status"] not in ("printed", "delivered"):
                raise ConflictError(f"纸 {prow['id']} 尚未洗成成品，不能交件")
            self.store.deliver_paper(prow["id"])
            products.append({"paper_id": prow["id"], "product": prow["product_path"],
                             "reused": bool(prow["reused"])})
        self.store.set_order_status(order_id, "delivered")
        self.store.set_batch_status(batch_id, "closed", loads(brow["plan_json"]))
        return {"order_id": order_id, "status": "delivered", "products": products}

    # ---------- 查询 ----------
    def status(self, batch_id: str) -> dict:
        brow = self.store.get_batch(batch_id)
        papers = []
        for prow in self.store.list_papers(batch_id):
            papers.append({
                "paper_id": prow["id"], "status": prow["status"],
                "reused": bool(prow["reused"]),
                "start": loads(prow["start_json"]),
                "accumulated": loads(prow["accumulated_json"]),
                "shots_logged": len(self.store.list_shots(prow["id"])),
                "product": prow["product_path"],
            })
        return {"batch_id": batch_id, "status": brow["status"],
                "darkroom": brow["darkroom"], "papers": papers,
                "plan": loads(brow["plan_json"])}

    # ---------- 内部工具 ----------
    def _load_order(self, batch_id: str):
        row = self.store.conn.execute(
            'SELECT * FROM "order" WHERE batch_id=?', (batch_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError(f"批次 {batch_id} 没有订单")
        return row

    def _requirements_by_paper(self, order_id: str) -> dict[str, list[dict]]:
        out: dict[str, list[dict]] = {}
        for row in self.store.list_requirements(order_id):
            out.setdefault(row["paper_id"], []).append({
                "cell": parse_cell(row["cell"]),
                "role": row["role"], "level": row["level"],
            })
        return out

    def _render_from_accumulated(self, prow, brow, suffix: str = "成品") -> dict:
        card = StepCard([StepLevel(**lv) for lv in loads(brow["card_json"])])
        paths = render_paper(
            self.product_dir, prow["id"], brow["rows"], brow["cols"],
            card, loads(prow["accumulated_json"]), suffix=suffix,
        )
        self.store.mark_paper_printed(prow["id"], dumps(paths))
        return paths
