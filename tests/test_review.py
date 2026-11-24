from __future__ import annotations

import unittest

from speed_camera_slowdown_evaluation.fixtures import camera, comparison_case, materials
from speed_camera_slowdown_evaluation.models import ReviewState
from speed_camera_slowdown_evaluation.regions import build_ledger
from speed_camera_slowdown_evaluation.review import (
    AssociationChange,
    assess_catalog_correction,
)


class CorrectionImpactTests(unittest.TestCase):
    def test_direction_correction_reports_lost_associations(self):
        before = comparison_case(direction="S", camera_direction="S")
        after = comparison_case(direction="S", camera_direction="N")
        impact = assess_catalog_correction(
            before, after, ledger=build_ledger(after)
        )
        self.assertTrue(impact.changed_windows)
        self.assertTrue(
            all(item.change is AssociationChange.LOST for item in impact.changed_windows)
        )
        self.assertEqual(
            {item.window_id for item in impact.changed_windows},
            {"WS-01", "WS-02", "WC-01", "WC-02"},
        )

    def test_effective_scope_goes_to_recheck(self):
        before = comparison_case()
        after = comparison_case(camera_direction="N")
        impact = assess_catalog_correction(
            before, after, ledger=build_ledger(after)
        )
        recheck = {item.road_id: item for item in impact.recheck_scopes}
        self.assertIn("RD-A", recheck)
        self.assertEqual(recheck["RD-A"].plan_ids, ("P-A",))
        self.assertTrue(any("lost" in reason for reason in recheck["RD-A"].reasons))

    def test_withdrawn_catalog_entry_marks_recheck_even_if_scope_unplanned(self):
        before = materials(
            cameras=(camera("C-A", "RD-A", "S", review_state=ReviewState.CONFIRMED),),
            windows=(),
            samples=(),
            plans=(),
        )
        after = materials(
            cameras=(camera("C-A", "RD-A", "S", review_state=ReviewState.WITHDRAWN),),
            windows=(),
            samples=(),
            plans=(),
        )
        impact = assess_catalog_correction(before, after)
        self.assertTrue(
            any("已撤回" in reason for item in impact.recheck_scopes for reason in item.reasons)
        )

    def test_limit_change_marks_recheck(self):
        before = materials(
            cameras=(camera("C-A", "RD-A", "S", limit_kph=50),),
            windows=(),
            samples=(),
        )
        after = materials(
            cameras=(camera("C-A", "RD-A", "S", limit_kph=30),),
            windows=(),
            samples=(),
        )
        impact = assess_catalog_correction(before, after)
        reasons = [reason for item in impact.recheck_scopes for reason in item.reasons]
        self.assertTrue(any("限速" in reason for reason in reasons))

    def test_historical_windows_are_preserved_verbatim(self):
        before = comparison_case()
        after = comparison_case(camera_direction="N")
        impact = assess_catalog_correction(before, after)
        history_ids = {item["window_id"] for item in impact.historical_windows}
        self.assertEqual(history_ids, {"WS-01", "WS-02", "WC-01", "WC-02"})
        for item in impact.historical_windows:
            self.assertIn("原始地图版本", item["note"])

    def test_unchanged_correction_leaves_windows_unaffected(self):
        case = comparison_case()
        impact = assess_catalog_correction(case, case, ledger=build_ledger(case))
        self.assertEqual(impact.changed_windows, ())
        self.assertEqual(impact.unaffected_windows, 4)


if __name__ == "__main__":
    unittest.main()
