"""Domain math tests (pure, no persistence)."""

import unittest

from app import model


def M(**kw):
    return model.Measurement(**kw)


class BiasMathTests(unittest.TestCase):
    def setUp(self):
        self.actual = {"P2": 2}      # +2 real offset
        self.nominal = {"P2": 0}     # sold/advertised as no offset

    def test_direct_read_subtracts_bias(self):
        m = M(board_id="A", ruler_id="R", method="direct",
              config={"start_piece": "P2"}, reading=12)
        self.assertEqual(m.true_length(self.actual), 10)
        self.assertEqual(m.true_length(self.nominal), 12)

    def test_same_piece_subtraction_cancels_bias(self):
        m = M(board_id="B", ruler_id="R", method="subtract",
              config={"start_piece": "P2"}, reading_a=12, reading_b=22)
        self.assertEqual(m.true_length(self.actual), 10)
        self.assertEqual(m.true_length(self.nominal), 10)
        # a subtraction on the same configuration is NOT bias sensitive
        self.assertFalse(m.bias_sensitive(self.actual, self.nominal))

    def test_extension_bias_adds_when_span_crosses_joint(self):
        actual = {"S": 2, "E": 1}
        m = M(board_id="C", ruler_id="R", method="direct",
              config={"start_piece": "S", "extension_piece": "E"},
              uses_extension=True, reading=15)
        self.assertEqual(m.true_length(actual), 12)   # 15 - (2+1)
        # without crossing the joint, extension bias is not carried
        m2 = M(board_id="C", ruler_id="R", method="direct",
               config={"start_piece": "S", "extension_piece": "E"},
               uses_extension=False, reading=15)
        self.assertEqual(m2.true_length(actual), 13)  # 15 - 2

    def test_subtract_with_different_configs_keeps_delta(self):
        actual = {"P2": 2, "P0": 0}
        m = M(board_id="D", ruler_id="R", method="subtract",
              config={"start_piece": "P2"},
              config_a={"start_piece": "P2"},
              config_b={"start_piece": "P0"},
              reading_a=12, reading_b=22)
        # a=(12-2)=10, b=(22-0)=22 -> 12
        self.assertEqual(m.true_length(actual), 12)

    def test_qualification_bounds(self):
        self.assertTrue(model.qualifies(10, 10, 10))
        self.assertTrue(model.qualifies(10, None, 10))
        self.assertTrue(model.qualifies(10, 10, None))
        self.assertFalse(model.qualifies(9, 10, None))
        self.assertFalse(model.qualifies(11, None, 10))

    def test_clearance_margin(self):
        self.assertEqual(model.clearance_margin(10, 10, 10), 0)
        self.assertEqual(model.clearance_margin(12, 10, None), 2)
        self.assertEqual(model.clearance_margin(8, 10, 12), -2)
        self.assertIsNone(model.clearance_margin(8, None, None))


class DecisionTests(unittest.TestCase):
    def test_release_when_qualifies(self):
        a, _ = model.decide_board_action(
            true_len=10, min_len=10, max_len=10, ref_reachable=True)
        self.assertEqual(a, model.KIND_RELEASE)

    def test_recheck_when_fails_and_reachable(self):
        a, _ = model.decide_board_action(
            true_len=12, min_len=10, max_len=10, ref_reachable=True)
        self.assertEqual(a, model.KIND_RECHECK)

    def test_recall_when_fails_and_covered(self):
        a, _ = model.decide_board_action(
            true_len=12, min_len=10, max_len=10, ref_reachable=False)
        self.assertEqual(a, model.KIND_RECALL)


if __name__ == "__main__":
    unittest.main()
