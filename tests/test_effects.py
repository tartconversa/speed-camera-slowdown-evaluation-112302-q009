from __future__ import annotations

import unittest

from speed_camera_slowdown_evaluation.effects import (
    MeasureStatus,
    Verdict,
    ZONE_LENGTH_M,
    evaluate_strategy,
)
from speed_camera_slowdown_evaluation.exposure import build_exposures
from speed_camera_slowdown_evaluation.fixtures import (
    camera,
    comparison_case,
    materials,
    straight_run,
    window,
)
from speed_camera_slowdown_evaluation.loader import load_materials
from speed_camera_slowdown_evaluation.models import TrafficState

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def analyze(case, **kwargs):
    return evaluate_strategy(case, build_exposures(case), **kwargs)


class EffectTests(unittest.TestCase):
    def test_within_scope_comparison_detects_slowdown(self):
        report = analyze(comparison_case())
        scope = report.scopes[0]
        self.assertEqual(scope.verdict, Verdict.ESTIMATED)
        # 策略两跑：60→50、62→51（减速 10、11）；对照两跑：60→57、61→58（3、3）。
        self.assertAlmostEqual(scope.strategy_slowdown_median_kph, 10.5)
        self.assertAlmostEqual(scope.control_slowdown_median_kph, 3.0)
        self.assertAlmostEqual(scope.effect_kph, 7.5)
        self.assertLess(scope.ci_low_kph, scope.effect_kph)
        self.assertGreater(scope.ci_high_kph, scope.effect_kph)

    def test_similar_slowdown_in_control_arm_is_inconclusive(self):
        report = analyze(
            comparison_case(
                strategy_runs=((60, 55, 50, 58), (62, 56, 51, 59)),
                control_runs=((60, 55, 50, 58), (61, 56, 51, 59)),
            )
        )
        scope = report.scopes[0]
        self.assertEqual(scope.verdict, Verdict.INCONCLUSIVE)
        self.assertAlmostEqual(scope.effect_kph, 0.5)

    def test_no_control_windows_yields_insufficient_control(self):
        report = analyze(comparison_case(control_runs=()))
        scope = report.scopes[0]
        self.assertEqual(scope.verdict, Verdict.INSUFFICIENT_CONTROL)
        self.assertIsNone(scope.effect_kph)
        self.assertEqual(scope.control_windows, 0)

    def test_no_strategy_windows_yields_insufficient_control(self):
        report = analyze(comparison_case(strategy_runs=()))
        scope = report.scopes[0]
        self.assertEqual(scope.verdict, Verdict.INSUFFICIENT_CONTROL)
        self.assertEqual(scope.strategy_windows, 0)

    def test_minimum_per_arm_is_enforced(self):
        report = analyze(
            comparison_case(
                strategy_runs=((60, 55, 50, 58),),
                control_runs=((60, 58, 57, 59),),
            )
        )
        scope = report.scopes[0]
        self.assertEqual(scope.verdict, Verdict.INSUFFICIENT_CONTROL)
        self.assertEqual(scope.comparable_strata, 0)

    def test_incident_windows_are_excluded_from_primary(self):
        case = comparison_case()
        incident_window = window(
            "WI-1",
            "RD-A",
            "S",
            vehicle_id="V-I",
            traffic_state=TrafficState.INCIDENT,
        )
        incident_samples = straight_run("WI-1", strategy_enabled=True)
        combined = materials(
            cameras=case.cameras,
            windows=case.windows + (incident_window,),
            samples=case.samples + incident_samples,
        )
        report = analyze(combined)
        profile = next(p for p in report.profiles if p.window_id == "WI-1")
        self.assertEqual(profile.status, MeasureStatus.EXCLUDED_INCIDENT)
        self.assertGreaterEqual(report.excluded_counts["incident_traffic_windows"], 1)
        # 交通事件窗口不进入策略臂计数。
        self.assertEqual(report.scopes[0].strategy_windows, 2)
        self.assertEqual(report.scopes[0].verdict, Verdict.ESTIMATED)

    def test_implausible_speed_drop_is_excluded_and_traced(self):
        case = comparison_case(
            strategy_runs=((60, 55, 50, 100), (62, 56, 51, 59)),
        )
        report = analyze(case)
        profile = report.profiles[0]
        self.assertEqual(profile.status, MeasureStatus.MEASURED)
        self.assertEqual(len(profile.excluded_samples), 1)
        self.assertEqual(profile.excluded_samples[0].speed_kph, 100.0)
        self.assertGreaterEqual(report.excluded_counts["implausible_samples"], 1)

    def test_traffic_state_or_weather_mismatch_prevents_pairing(self):
        case = comparison_case()
        wet_control = window("WC-01", "RD-A", "S", vehicle_id="V-C0", weather="wet")
        combined = materials(
            cameras=case.cameras,
            windows=tuple(
                wet_control if item.window_id == "WC-01" else item
                for item in case.windows
            ),
            samples=case.samples,
        )
        report = analyze(combined)
        self.assertEqual(report.scopes[0].verdict, Verdict.INSUFFICIENT_CONTROL)
        self.assertEqual(report.scopes[0].comparable_strata, 0)

    def test_cross_scope_sensitivity_needs_two_roads_per_arm(self):
        base = comparison_case()
        other = comparison_case()
        # 同一交通/天气/采样率，但另一条道路；单道路时跨道路分层不成立。
        combined = materials(
            cameras=base.cameras + (camera("C-B", "RD-B", "S"),),
            windows=base.windows
            + tuple(
                window(item.window_id.replace("W", "B"), "RD-B", "S", vehicle_id=item.vehicle_id + "B")
                for item in other.windows
            ),
            samples=base.samples
            + tuple(
                type(sample)(
                    window_id=sample.window_id.replace("W", "B"),
                    offset_m=sample.offset_m,
                    speed_kph=sample.speed_kph,
                    strategy_enabled=sample.strategy_enabled,
                    camera_seen=sample.camera_seen,
                    map_version=sample.map_version,
                )
                for sample in other.samples
            ),
        )
        report = analyze(combined)
        self.assertEqual(len(report.scopes), 2)
        self.assertEqual(report.cross_scope[0].verdict, Verdict.ESTIMATED)
        self.assertEqual(report.cross_scope[0].analysis, "cross_scope")

    def test_limit_grouping_marks_ambiguous_limits(self):
        extra = camera("C-A2", "RD-A", "S", limit_kph=70, map_version="M-1")
        case = comparison_case(extra_cameras=(extra,))
        report = analyze(case)
        self.assertEqual(report.scopes[0].limit_kph, None)
        self.assertIn("多个限速", report.scopes[0].note)

    def test_baseline_has_no_within_scope_pairs(self):
        load = load_materials(ROOT / "data")
        exposure = build_exposures(load.materials)
        report = evaluate_strategy(load.materials, exposure)
        self.assertEqual(report.overall.verdict, Verdict.INSUFFICIENT_CONTROL)
        self.assertEqual(report.overall.comparable_strata, 0)
        self.assertTrue(
            all(
                scope.verdict is Verdict.INSUFFICIENT_CONTROL
                for scope in report.scopes
            )
        )
        # 基线中存在事件窗口与异常样本，二者都必须体现为排除计数。
        self.assertGreater(report.excluded_counts["incident_traffic_windows"], 0)
        self.assertGreater(report.excluded_counts["implausible_samples"], 0)

    def test_degenerate_resampling_is_inconclusive(self):
        # 同一臂的所有窗口结果完全相同，分层内重采样没有变异，不能宣称已估出效应。
        report = analyze(
            comparison_case(
                strategy_runs=((60, 55, 50, 58), (60, 55, 50, 58)),
                control_runs=((60, 58, 57, 59), (60, 58, 57, 59)),
            )
        )
        scope = report.scopes[0]
        self.assertEqual(scope.verdict, Verdict.INCONCLUSIVE)
        self.assertIn("退化", scope.note)

    def test_zone_length_is_used_for_zone_selection(self):
        report = analyze(comparison_case())
        profile = report.profiles[0]
        self.assertEqual(profile.detection_offset_m, 25.0)
        self.assertEqual(ZONE_LENGTH_M, 50.0)
        self.assertEqual(profile.zone_min_speed_kph, 50.0)


if __name__ == "__main__":
    unittest.main()
