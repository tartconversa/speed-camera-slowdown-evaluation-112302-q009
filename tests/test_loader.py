from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from speed_camera_slowdown_evaluation.loader import (
    MaterialSchemaError,
    load_materials,
    read_snapshot,
)
from speed_camera_slowdown_evaluation.reporting import import_snapshot

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"


class LoaderTests(unittest.TestCase):
    def test_loads_baseline_without_contract_errors(self):
        result = load_materials(DATA)
        self.assertEqual(result.row_counts["camera_catalog.csv"], 35)
        self.assertEqual(result.row_counts["trajectory_windows.csv"], 48)
        self.assertEqual(result.row_counts["speed_samples.csv"], 192)
        self.assertEqual(result.row_counts["region_plans.csv"], 21)
        self.assertFalse([issue for issue in result.issues if issue.severity == "error"])

    def test_counts_duplicate_samples_and_keeps_first(self):
        with tempfile.TemporaryDirectory() as tmp:
            copy = Path(tmp) / "data"
            shutil.copytree(DATA, copy)
            sample_path = copy / "speed_samples.csv"
            lines = sample_path.read_text(encoding="utf-8-sig").splitlines()
            lines.insert(2, "W-001,0,999,no,no,M-1")
            sample_path.write_text("﻿" + "\n".join(lines) + "\n", encoding="utf-8")
            result = load_materials(copy)
        codes = [issue.code for issue in result.issues]
        self.assertIn("duplicate_sample_offset", codes)
        first = next(
            sample
            for sample in result.materials.samples
            if sample.window_id == "W-001" and sample.offset_m == 0
        )
        self.assertNotEqual(first.speed_kph, 999)

    def test_duplicate_window_is_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            copy = Path(tmp) / "data"
            shutil.copytree(DATA, copy)
            path = copy / "trajectory_windows.csv"
            lines = path.read_text(encoding="utf-8-sig").splitlines()
            lines.append(lines[2])
            path.write_text("﻿" + "\n".join(lines) + "\n", encoding="utf-8")
            result = load_materials(copy)
        codes = [issue.code for issue in result.issues if issue.severity == "error"]
        self.assertIn("duplicate_window_id", codes)

    def test_bad_direction_is_rejected_per_row(self):
        with tempfile.TemporaryDirectory() as tmp:
            copy = Path(tmp) / "data"
            shutil.copytree(DATA, copy)
            path = copy / "camera_catalog.csv"
            lines = path.read_text(encoding="utf-8-sig").splitlines()
            fields = lines[2].split(",")
            fields[2] = "X"
            lines[2] = ",".join(fields)
            path.write_text("﻿" + "\n".join(lines) + "\n", encoding="utf-8")
            result = load_materials(copy)
        self.assertIn("camera_parse_error", [issue.code for issue in result.issues])
        self.assertNotIn("C-002", {camera.camera_id for camera in result.materials.cameras})

    def test_orphan_sample_is_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            copy = Path(tmp) / "data"
            shutil.copytree(DATA, copy)
            path = copy / "speed_samples.csv"
            lines = path.read_text(encoding="utf-8-sig").splitlines()
            lines.insert(2, "W-999,0,50,no,no,M-1")
            path.write_text("﻿" + "\n".join(lines) + "\n", encoding="utf-8")
            result = load_materials(copy)
        self.assertIn("orphan_sample", [issue.code for issue in result.issues])
        self.assertNotIn("W-999", {sample.window_id for sample in result.materials.samples})

    def test_schema_error_for_missing_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(MaterialSchemaError):
                load_materials(tmp)

    def test_snapshot_round_trip(self):
        load = load_materials(DATA)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "snapshot.json"
            import_snapshot(load.materials, load, path)
            reloaded = read_snapshot(path)
        self.assertEqual(len(reloaded.materials.cameras), len(load.materials.cameras))
        self.assertEqual(len(reloaded.materials.samples), len(load.materials.samples))
        self.assertEqual(
            [issue.as_dict() for issue in reloaded.issues],
            [issue.as_dict() for issue in load.issues],
        )


if __name__ == "__main__":
    unittest.main()
