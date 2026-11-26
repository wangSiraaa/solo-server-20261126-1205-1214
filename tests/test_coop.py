from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from coop.db import connect, init_db
from coop.service import CoopService


class CoopDomainTest(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = connect(":memory:")
        init_db(self.conn)
        self.service = CoopService(self.conn)
        self.setup_dual_case("T")

    def setup_dual_case(self, slug: str) -> None:
        s = self.service
        self.customer = f"C-{slug}"
        self.old_line = f"OLD-{slug}"
        self.new_line = f"NEW-{slug}"
        self.case_id = f"CASE-{slug}"
        self.new_outage = f"OUT-{slug}-NEW"

        s.create_customer(self.customer, "测试住户", "client-1")
        s.create_line(self.old_line, self.customer, "old", "旧屋", 0, "client-1", returned_at=9)
        s.create_line(self.new_line, self.customer, "new", "新套间", 3, "client-2")
        s.add_commitment(f"CMT-{slug}-OLD", self.old_line, 0, 9, "client-1", kind="promised")
        s.add_commitment(f"CMT-{slug}-NEW", self.new_line, 4, 12, "client-2", kind="promised")
        s.record_fault_report(
            f"RPT-{slug}-OLD", self.old_line, 4, "client-1",
            end_minute=8, fault_key=f"{slug}-OLD",
        )
        s.record_fault_report(
            f"RPT-{slug}-NEW", self.new_line, 6, "client-2",
            end_minute=7, fault_key=f"{slug}-NEW",
        )
        # A second report for the same old fault must not create another outage.
        duplicate = s.record_fault_report(
            f"RPT-{slug}-OLD-2", self.old_line, 4, "client-2",
            end_minute=8, fault_key=f"{slug}-OLD",
        )
        self.assertTrue(duplicate["duplicate_fault"])
        s.run_compensation(self.case_id, self.customer, 0, 12, "client-1")

    def test_dual_promise_all_points_down_only_one_minute(self) -> None:
        case = self.service.get_case(self.case_id)
        latest = case["versions"][-1]["calculation"]
        self.assertEqual(1, latest["expected_minutes"])
        self.assertEqual([[6, 7]], latest["owed_intervals"])
        owed = [row for row in latest["minutes"] if row["owed"]]
        self.assertEqual([6], [row["minute"] for row in owed])

    def test_test_only_new_access_restores_four_old_minutes(self) -> None:
        self.service.add_evidence(
            "EV-TEST", "commitment_correction", "4-8 test only",
            "client-1", customer_id=self.customer,
        )
        self.service.correct_commitment_to_test(
            self.new_line, 4, 8, "EV-TEST", "client-1", "CMT-FIX"
        )
        case = self.service.recalculate_compensation(
            self.case_id, "client-2", evidence_id="EV-TEST"
        )
        latest = case["versions"][-1]["calculation"]
        self.assertEqual(4, latest["expected_minutes"])
        self.assertEqual([[4, 8]], latest["owed_intervals"])
        self.assertEqual(2, len(case["vouchers"]))
        self.assertEqual(3, case["vouchers"][-1]["granted_minutes"])

    def test_late_full_availability_cancels_unused_voucher(self) -> None:
        self.service.add_evidence(
            "EV-ZERO", "availability_correction", "actually up",
            "client-1", customer_id=self.customer,
        )
        self.service.correct_outage(self.new_outage, "EV-ZERO", "client-1")
        case = self.service.recalculate_compensation(
            self.case_id, "client-2", evidence_id="EV-ZERO"
        )
        self.assertEqual(0, case["versions"][-1]["expected_minutes"])
        voucher = case["vouchers"][0]
        self.assertEqual(1, voucher["cancelled_minutes"])
        self.assertEqual("cancelled", voucher["status"])

    def test_redeemed_voucher_is_charged_to_same_renewal_and_receipt_is_idempotent(self) -> None:
        case = self.service.get_case(self.case_id)
        voucher_id = case["vouchers"][0]["id"]
        params = dict(
            receipt_id="RCPT-1",
            customer_id=self.customer,
            order_id="RNW-1",
            service_start=10,
            service_minutes=30,
            rate_minor_per_minute=1,
            voucher_id=voucher_id,
            voucher_minutes=1,
            client_id="client-2",
        )
        first = self.service.receive_renewal_receipt(**params)
        self.assertFalse(first["duplicate_receipt"])
        self.assertEqual(0, first["balance_minor"])

        duplicate = self.service.receive_renewal_receipt(**params)
        self.assertTrue(duplicate["duplicate_receipt"])
        self.assertEqual(2, duplicate["receipt"]["delivery_count"])
        self.assertEqual(3, len(duplicate["account_entries"]))

        self.service.add_evidence(
            "EV-CLAW", "late_repair", "new was actually up",
            "client-1", customer_id=self.customer,
        )
        self.service.correct_outage(self.new_outage, "EV-CLAW", "client-1")
        corrected_case = self.service.recalculate_compensation(
            self.case_id, "client-1", evidence_id="EV-CLAW", as_of_minute=12
        )
        self.assertEqual(0, corrected_case["versions"][-1]["expected_minutes"])

        renewal = self.service.get_renewal("RNW-1", "client-2", as_of_minute=12)
        self.assertEqual(1, renewal["balance_minor"])
        kinds = [entry["kind"] for entry in renewal["account_entries"]]
        self.assertEqual(
            ["renewal_charge", "voucher_discount", "payment", "voucher_clawback_charge"],
            kinds,
        )
        clawback = renewal["account_entries"][-1]
        self.assertEqual(1, clawback["amount_minor"])
        self.assertEqual([10, 12], clawback["detail"]["performed_half_open_interval"])
        self.assertEqual(2, clawback["detail"]["performed_minutes"])

        # The same receipt after correction must not add another payment/discount.
        third = self.service.receive_renewal_receipt(**params)
        self.assertEqual(3, third["receipt"]["delivery_count"])
        self.assertEqual(4, len(third["account_entries"]))
        self.assertEqual(1, third["balance_minor"])

        paid = self.service.pay_outstanding_balance("RNW-1", "client-1", payment_id="PAY-1")
        self.assertEqual(0, paid["balance_minor"])
        self.assertEqual(10, paid["order"]["service_start"])
        self.assertEqual(40, paid["order"]["service_end"])

        notices = self.service.list_notifications(self.customer, "client-1")["notifications"]
        self.assertTrue(any(n["kind"] == "clawback_notice" for n in notices))

    def test_late_repair_shortens_outage_and_cancels_difference(self) -> None:
        # Rebuild a case where the new outage was initially believed [6,8).
        slug = "LATE"
        self.setup_dual_case(slug)
        # The standard helper reports [6,7). Add a second, longer fault key to
        # exercise correction from [6,8) to [6,7) in a fresh case.
        self.service.record_fault_report(
            "RPT-LATE-LONG", "NEW-LATE", 6, "client-2",
            end_minute=8, fault_key="LATE-LONG",
        )
        self.service.run_compensation("CASE-LATE-LONG", "C-LATE", 0, 12, "client-1")
        long_case = self.service.get_case("CASE-LATE-LONG")
        # Existing [6,7) plus long [6,8) union remains [6,8): two owed minutes.
        self.assertEqual(2, long_case["versions"][-1]["expected_minutes"])

        self.service.add_evidence(
            "EV-REPAIR", "late_repair_record", "repair at 7",
            "client-1", customer_id="C-LATE",
        )
        self.service.correct_outage(
            "OUT-LATE-LONG", "EV-REPAIR", "client-1",
            new_status="corrected", new_end_minute=7,
        )
        corrected = self.service.recalculate_compensation(
            "CASE-LATE-LONG", "client-1", evidence_id="EV-REPAIR"
        )
        self.assertEqual(1, corrected["versions"][-1]["expected_minutes"])
        voucher = corrected["vouchers"][0]
        self.assertEqual(1, voucher["cancelled_minutes"])

    def test_persistence_across_reconnect(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "restart.sqlite3"
            conn = connect(db)
            init_db(conn)
            service = CoopService(conn)
            service.create_customer("C-P", "持久化", "terminal-1")
            service.create_line("OLD-P", "C-P", "old", "旧", 0, "terminal-1", returned_at=9)
            service.create_line("NEW-P", "C-P", "new", "新", 3, "terminal-2")
            service.add_commitment("A", "OLD-P", 4, 8, "terminal-1")
            service.add_commitment("B", "NEW-P", 4, 8, "terminal-2")
            service.record_fault_report("R1", "OLD-P", 4, "terminal-1", end_minute=8, fault_key="P1")
            service.record_fault_report("R2", "NEW-P", 6, "terminal-2", end_minute=7, fault_key="P2")
            service.run_compensation("CASE-P", "C-P", 0, 12, "terminal-1")
            case = service.get_case("CASE-P")
            service.receive_renewal_receipt(
                "RCPT-P", "C-P", 10, "terminal-2",
                order_id="RNW-P", voucher_id=case["vouchers"][0]["id"],
            )
            conn.close()

            restarted = CoopService(connect(db))
            renewal = restarted.get_renewal("RCPT-P", "terminal-2")
            self.assertEqual("RNW-P", renewal["order"]["id"])
            self.assertEqual(1, renewal["order"]["voucher_minutes_applied"])
            self.assertEqual(0, renewal["balance_minor"])
            case_after = restarted.get_case("CASE-P")
            self.assertEqual(1, case_after["versions"][0]["expected_minutes"])


if __name__ == "__main__":
    unittest.main()
