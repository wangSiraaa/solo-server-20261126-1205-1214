"""Shared test harness."""

from __future__ import annotations

import os
import tempfile
import unittest

from lampshop.db import Database
from lampshop.service import DomainService

# Canonical timestamps for the narrative in the spec.
T = {
    "root": "2026-01-01T00:00:00Z",
    "reg_ab": "2026-01-02T08:00:00Z",       # book filled before people arrive
    "ab_start": "2026-01-05T09:00:00Z",
    "ab_end": "2026-01-05T13:00:00Z",       # 4h
    "b_qual": "2026-01-06T00:00:00Z",
    "reg_bc": "2026-01-10T00:00:00Z",
    "bc_start": "2026-01-10T09:00:00Z",
    "bc_end": "2026-01-10T13:00:00Z",       # 4h
    "c_qual": "2026-01-11T00:00:00Z",
    "sign": "2026-01-12T10:00:00Z",
    "shift_start": "2026-01-12T08:00:00Z",
    "shift_end": "2026-01-12T18:00:00Z",
}


class WorkshopTestCase(unittest.TestCase):
    """Builds the canonical 甲->乙->丙 workshop in a fresh temp DB."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "workshop.db")
        self.db = Database(self.db_path)
        self.svc = DomainService(self.db)
        self._build_baseline()

    def tearDown(self):
        self.tmp.cleanup()

    # -- fixtures ---------------------------------------------------------
    def _build_baseline(self):
        s = self.svc
        s.create_person("甲", "A", role="mentor")
        s.create_person("乙", "B", role="apprentice")
        s.create_person("丙", "C", role="apprentice")
        s.create_person("丁", "D", role="mentor")
        s.create_craft("shade", "灯罩", min_minutes=120)
        s.add_craft_step("shade", "cut", "裁片", 1)
        s.add_craft_step("shade", "sew", "缝制", 2)
        s.create_product_type("lamp", "灯罩成品", ["shade"])
        s.create_station("bench1", "一号台")
        s.create_station("bench2", "二号台")
        s.create_post("signer", "签收岗", sign_authority=True)
        s.create_post("apprentice_post", "学徒岗", sign_authority=False)
        # 甲 is the initial mentor: root production+mentoring authority.
        s.seed_root("A", ["shade"], T["root"])
        # 乙 learns both steps from 甲 (the canonical first apprenticeship).
        ev = s.register_evidence(
            "shade", T["ab_start"], T["ab_end"], ["A"], ["B"], ["cut", "sew"],
            station_id="bench1", registered_at=T["reg_ab"], evidence_id="ev-ab")
        s.complete_evidence("ev-ab", T["ab_end"])

    # -- helpers ----------------------------------------------------------
    def learn(self, craft, mentor, student, start, end, steps=None, station="bench1",
              registered_at=None, kind="apprenticeship", mentor_steps=None,
              evidence_id=None, completed_at=None):
        ev = self.svc.register_evidence(
            craft, start, end, [mentor], [student],
            steps if steps is not None else ["cut", "sew"],
            station_id=station, kind=kind, registered_at=registered_at,
            mentor_steps=mentor_steps, evidence_id=evidence_id,
        )
        return self.svc.complete_evidence(ev["id"], completed_at or end)

    def grant(self, person, craft="shade", cap="production", vf="2026-01-06T00:00:00Z",
              vu=None, enforce=True, source_kind=None):
        return self.svc.grant_qualification(
            person, craft, cap, vf, vu, source_kind=source_kind, enforce_eligibility=enforce)

    def make_bc(self):
        """Canonical: 乙 got mentoring authority, then teaches 丙."""
        self.grant("B", cap="production", vf=T["b_qual"])
        self.grant("B", cap="mentoring", vf=T["b_qual"], enforce=False)
        return self.learn("shade", "B", "C", T["bc_start"], T["bc_end"],
                          station="bench1", registered_at=T["reg_bc"])

    def ready_to_sign(self):
        """Canonical production-ready 丙 on the signing post."""
        self.make_bc()
        self.grant("C", cap="production", vf=T["c_qual"])
        self.svc.create_shift("C", "signer", T["shift_start"], T["shift_end"])

    def sign(self, product="lamp1", signer="C", request_id="req-1", at=T["sign"]):
        try:
            self.svc.create_product("lamp", product)
        except Exception:
            pass
        return self.svc.sign_product(product, signer, request_id, at_ts=at)
