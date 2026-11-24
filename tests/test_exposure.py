from __future__ import annotations

import unittest

from speed_camera_slowdown_evaluation.exposure import (
    ExposureStatus,
    build_exposures,
)
from speed_camera_slowdown_evaluation.fixtures import (
    camera,
    comparison_case,
    materials,
    straight_run,
    window,
)
from speed_camera_slowdown_evaluation.models import ReviewState


class ExposureTests(unittest.TestCase):
    def test_comparison_case_is_fully_eligible(self):
        exposure = build_exposures(comparison_case())
        self.assertEqual(exposure.counts, {"eligible": 4})
        arms = sorted(item.arm for item in exposure.exposures)
        self.assertEqual(arms, ["control", "control", "strategy", "strategy"])

    def test_wrong_direction_camera_is_not_used_as_exposure(self):
        # 目录只给出对向条目：车辆实际经过同向道路，不能借用对向摄像头。
        case = comparison_case(camera_direction="N", direction="S")
        exposure = build_exposures(case)
        self.assertEqual(
            {item.status for item in exposure.exposures},
            {ExposureStatus.DIRECTION_MISMATCH},
        )
        self.assertTrue(
            all(item.camera_ids == () for item in exposure.exposures),
            "方向不一致的条目不能出现在窗口关联中",
        )

    def test_needs_review_camera_does_not_create_exposure(self):
        case = materials(
            cameras=(
                camera("C-R", "RD-A", "S", review_state=ReviewState.NEEDS_REVIEW),
            ),
            windows=(window("W-1", "RD-A", "S"),),
            samples=straight_run("W-1", strategy_enabled=True),
        )
        exposure = build_exposures(case)
        self.assertEqual(exposure.exposures[0].status, ExposureStatus.NO_CONFIRMED_CAMERA)

    def test_withdrawn_camera_does_not_create_exposure(self):
        case = materials(
            cameras=(
                camera("C-X", "RD-A", "S", review_state=ReviewState.WITHDRAWN),
            ),
            windows=(window("W-1", "RD-A", "S"),),
            samples=straight_run("W-1", strategy_enabled=False),
        )
        exposure = build_exposures(case)
        self.assertEqual(exposure.exposures[0].status, ExposureStatus.NO_CONFIRMED_CAMERA)

    def test_catalog_entry_without_vehicle_detection_is_not_exposure(self):
        case = materials(
            cameras=(camera("C-A", "RD-A", "S"),),
            windows=(window("W-1", "RD-A", "S"),),
            samples=straight_run("W-1", strategy_enabled=True, camera_seen=False),
        )
        exposure = build_exposures(case)
        self.assertEqual(exposure.exposures[0].status, ExposureStatus.CAMERA_NOT_OBSERVED)
        self.assertEqual(exposure.eligible, ())

    def test_map_version_mismatch_is_reported(self):
        case = materials(
            cameras=(camera("C-A", "RD-A", "S", map_version="M-1"),),
            windows=(window("W-1", "RD-A", "S"),),
            samples=straight_run("W-1", strategy_enabled=True, map_version="M-2"),
        )
        exposure = build_exposures(case)
        self.assertEqual(exposure.exposures[0].status, ExposureStatus.MAP_VERSION_MISMATCH)

    def test_multiple_catalog_entries_count_as_one_window_exposure(self):
        case = comparison_case(
            extra_cameras=(camera("C-B", "RD-A", "S", km_marker=200.0),)
        )
        exposure = build_exposures(case)
        eligible = exposure.eligible
        self.assertTrue(all(item.camera_entry_count == 2 for item in eligible))
        self.assertEqual(len(eligible), 4)
        self.assertEqual(
            {item.camera_ids for item in eligible},
            {("C-A", "C-B")},
        )

    def test_camera_only_road_reports_zero_exposure_not_zero_events(self):
        case = materials(
            cameras=(camera("C-Z", "RD-Z", "N"),),
            windows=(),
            samples=(),
        )
        exposure = build_exposures(case)
        scope = exposure.scope_coverage[0]
        self.assertEqual(scope.camera_entries, 1)
        self.assertEqual(scope.eligible_windows, 0)
        self.assertEqual(scope.windows, 0)
        self.assertIn("没有轨迹窗口", scope.note)

    def test_mixed_strategy_state_window_is_not_comparable(self):
        samples = list(straight_run("W-1", strategy_enabled=True))
        samples[-1] = type(samples[-1])(
            window_id="W-1",
            offset_m=75.0,
            speed_kph=58.0,
            strategy_enabled=False,
            camera_seen=True,
            map_version="M-1",
        )
        case = materials(
            cameras=(camera("C-A", "RD-A", "S"),),
            windows=(window("W-1", "RD-A", "S"),),
            samples=tuple(samples),
        )
        exposure = build_exposures(case)
        self.assertEqual(exposure.exposures[0].status, ExposureStatus.MIXED_STRATEGY)

    def test_window_without_after_samples_is_incomplete_geometry(self):
        samples = tuple(
            sample
            for sample in straight_run("W-1", strategy_enabled=True)
            if sample.offset_m <= 25
        )
        case = materials(
            cameras=(camera("C-A", "RD-A", "S"),),
            windows=(window("W-1", "RD-A", "S"),),
            samples=samples,
        )
        exposure = build_exposures(case)
        self.assertEqual(exposure.exposures[0].status, ExposureStatus.INCOMPLETE_GEOMETRY)

    def test_control_windows_remain_eligible_without_strategy(self):
        case = comparison_case(strategy_runs=())
        exposure = build_exposures(case)
        self.assertEqual(exposure.counts, {"eligible": 2})
        self.assertTrue(all(item.arm == "control" for item in exposure.eligible))


if __name__ == "__main__":
    unittest.main()
