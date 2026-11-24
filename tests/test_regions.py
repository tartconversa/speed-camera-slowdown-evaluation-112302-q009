from __future__ import annotations

import unittest
from datetime import date
from pathlib import Path

from speed_camera_slowdown_evaluation.cli import _scope_counts
from speed_camera_slowdown_evaluation.effects import evaluate_strategy
from speed_camera_slowdown_evaluation.exposure import build_exposures
from speed_camera_slowdown_evaluation.fixtures import comparison_case, materials, plan
from speed_camera_slowdown_evaluation.loader import load_materials
from speed_camera_slowdown_evaluation.models import PlanStatus
from speed_camera_slowdown_evaluation.regions import (
    Decision,
    ScopeState,
    build_ledger,
    recommend_regions,
    withdraw_road_direction,
    withdraw_scope,
)

ROOT = Path(__file__).resolve().parents[1]


class LedgerTests(unittest.TestCase):
    def test_baseline_parses_effective_and_proposed(self):
        ledger = build_ledger(load_materials(ROOT / "data").materials)
        effective = ledger.effective
        proposed = ledger.proposed
        self.assertTrue(effective)
        self.assertTrue(proposed)
        self.assertTrue(all(item.state is ScopeState.EFFECTIVE for item in effective))
        self.assertTrue(all(item.state is ScopeState.PROPOSED for item in proposed))

    def test_newer_version_supersedes_older(self):
        case = materials(
            plans=(
                plan("P-1", "RD-A", "S", version="V1", effective_from=date(2026, 1, 1)),
                plan("P-2", "RD-A", "S", version="V2", effective_from=date(2026, 2, 1)),
            )
        )
        ledger = build_ledger(case)
        states = {item.plan_id: item for item in ledger.scopes}
        self.assertEqual(states["P-2"].state, ScopeState.EFFECTIVE)
        self.assertEqual(states["P-1"].state, ScopeState.SUPERSEDED)
        self.assertEqual(states["P-1"].superseded_by, "P-2")

    def test_withdrawal_record_overrides_effective_plan(self):
        case = materials(
            plans=(
                plan("P-1", "RD-A", "S", effective_from=date(2026, 1, 1)),
                plan(
                    "P-2",
                    "RD-A",
                    "S",
                    status=PlanStatus.WITHDRAWN,
                    effective_from=date(2026, 3, 1),
                ),
            )
        )
        ledger = build_ledger(case)
        states = {item.plan_id: item for item in ledger.scopes}
        self.assertEqual(states["P-1"].state, ScopeState.WITHDRAWN)
        self.assertEqual(states["P-1"].withdrawn_by, "P-2")

    def test_opposite_direction_is_not_affected_by_recommendations(self):
        case = comparison_case(direction="S")
        other = plan("P-B", "RD-A", "N", region="R-N", receipt_id="RC-2")
        with_other = materials(
            cameras=case.cameras, windows=case.windows, samples=case.samples,
            plans=case.plans + (other,),
        )
        exposure = build_exposures(with_other)
        effects = evaluate_strategy(with_other, exposure)
        ledger = build_ledger(with_other)
        recommendations = {
            item.plan_id: item
            for item in recommend_regions(ledger, effects, _scope_counts(exposure))
        }
        self.assertEqual(recommendations["P-A"].decision, Decision.LIMITED)
        # 对向没有轨迹与暴露，保持暂缓，而不是借用另一方向的结论。
        self.assertEqual(recommendations["P-B"].decision, Decision.HOLD)

    def test_proposed_scope_never_continues(self):
        case = comparison_case()
        proposed = plan("P-P", "RD-A", "S", status=PlanStatus.PROPOSED)
        case = materials(
            cameras=case.cameras, windows=case.windows, samples=case.samples,
            plans=(proposed,),
        )
        exposure = build_exposures(case)
        effects = evaluate_strategy(case, exposure)
        ledger = build_ledger(case)
        recommendation = recommend_regions(ledger, effects, _scope_counts(exposure))[0]
        self.assertEqual(recommendation.decision, Decision.HOLD)
        self.assertEqual(recommendation.scope_state, "proposed")

    def test_scope_without_control_holds(self):
        case = comparison_case(control_runs=())
        exposure = build_exposures(case)
        effects = evaluate_strategy(case, exposure)
        ledger = build_ledger(case)
        recommendation = recommend_regions(ledger, effects, _scope_counts(exposure))[0]
        self.assertEqual(recommendation.decision, Decision.HOLD)
        self.assertIn("缺少未启用对照", recommendation.reasons[0])


class WithdrawalTests(unittest.TestCase):
    def _ledger(self):
        return build_ledger(
            materials(
                windows=(),
                samples=(),
                plans=(
                    plan("P-S1", "RD-A", "S", effective_from=date(2026, 1, 1)),
                    plan("P-N1", "RD-A", "N", effective_from=date(2026, 1, 2)),
                    plan("P-S2", "RD-B", "S", effective_from=date(2026, 1, 3)),
                ),
            )
        )

    def test_partial_withdrawal_keeps_opposite_and_other_roads(self):
        result = withdraw_road_direction(self._ledger(), "RD-A", "S")
        self.assertEqual(result.affected_plan_ids, ("P-S1",))
        retained = {item.plan_id for item in result.retained}
        self.assertEqual(retained, {"P-N1", "P-S2"})
        self.assertIn("同道路对向", result.note)

    def test_single_plan_withdrawal_leaves_rest_effective(self):
        result = withdraw_scope(self._ledger(), "P-S2")
        self.assertEqual(result.affected_plan_ids, ("P-S2",))
        self.assertEqual(
            {item.plan_id for item in result.retained}, {"P-S1", "P-N1"}
        )

    def test_repeated_withdrawal_is_idempotent(self):
        ledger = self._ledger()
        first = withdraw_scope(ledger, "P-S1")
        self.assertFalse(first.already_withdrawn)
        already = build_ledger(
            materials(
                windows=(),
                samples=(),
                plans=(
                    plan("P-S1", "RD-A", "S", effective_from=date(2026, 1, 1)),
                    plan(
                        "P-N1", "RD-A", "N", effective_from=date(2026, 1, 2)
                    ),
                    plan("P-S1X", "RD-A", "S", status=PlanStatus.WITHDRAWN,
                         effective_from=date(2026, 6, 1)),
                ),
            )
        )
        repeat = withdraw_scope(already, "P-S1")
        self.assertTrue(repeat.already_withdrawn)
        self.assertEqual(repeat.affected_plan_ids, ())

    def test_withdrawal_keeps_historical_window_ids(self):
        case = comparison_case()
        ledger = build_ledger(case)
        result = withdraw_scope(ledger, "P-A")
        self.assertIn("WS-01", result.historical_window_ids)
        self.assertIn("WC-01", result.historical_window_ids)

    def test_version_mismatch_raises(self):
        with self.assertRaises(KeyError):
            withdraw_scope(self._ledger(), "P-S1", version="V99")

    def test_unknown_plan_raises(self):
        with self.assertRaises(KeyError):
            withdraw_scope(self._ledger(), "P-nope")


if __name__ == "__main__":
    unittest.main()
