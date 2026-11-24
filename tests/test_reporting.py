from __future__ import annotations

import json
import unittest
from pathlib import Path

from speed_camera_slowdown_evaluation.cli import _scope_counts
from speed_camera_slowdown_evaluation.effects import evaluate_strategy
from speed_camera_slowdown_evaluation.exposure import build_exposures
from speed_camera_slowdown_evaluation.loader import load_materials
from speed_camera_slowdown_evaluation.regions import build_ledger, recommend_regions
from speed_camera_slowdown_evaluation.reporting import analysis_report, coverage_gaps

ROOT = Path(__file__).resolve().parents[1]


class ReportingTests(unittest.TestCase):
    def _report(self):
        load = load_materials(ROOT / "data")
        exposure = build_exposures(load.materials)
        effects = evaluate_strategy(load.materials, exposure)
        ledger = build_ledger(load.materials)
        counts = _scope_counts(exposure)
        recommendations = recommend_regions(ledger, effects, counts)
        return analysis_report(
            load, exposure, effects, ledger, recommendations, scope_counts=counts
        )

    def test_report_is_json_serialisable(self):
        report = self._report()
        encoded = json.dumps(report, ensure_ascii=False)
        self.assertIn("disclaimer", encoded)
        decoded = json.loads(encoded)
        self.assertEqual(decoded["summary"]["windows_total"], 48)

    def test_report_includes_quality_and_coverage_sections(self):
        report = self._report()
        self.assertIn("data_quality", report)
        self.assertIn("coverage_gaps", report)
        self.assertTrue(report["coverage_gaps"])

    def test_coverage_gaps_state_reason_per_scope(self):
        exposure = build_exposures(load_materials(ROOT / "data").materials)
        gaps = coverage_gaps(exposure)
        reasons = {
            (item["road_id"], item["direction"]): item["reasons"] for item in gaps
        }
        self.assertIn(("RD-00", "N"), reasons)
        self.assertIn("缺少未启用对照窗口。", reasons[("RD-00", "N")])
        self.assertIn(("RD-01", "S"), reasons)
        self.assertTrue(
            any("没有窗口" in reason for reason in reasons[("RD-01", "S")])
        )

    def test_disclaimer_is_present(self):
        report = self._report()
        self.assertIn("不表示新闻已报道", report["disclaimer"])


if __name__ == "__main__":
    unittest.main()
