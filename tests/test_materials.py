from __future__ import annotations

import csv
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]


class MaterialTests(unittest.TestCase):
    def test_files_have_declared_headers_and_rows(self):
        expected = EXPECTED
        for filename, (columns, minimum_rows) in expected.items():
            with self.subTest(filename=filename):
                with (ROOT / "data" / filename).open(encoding="utf-8-sig", newline="") as stream:
                    reader = csv.DictReader(stream)
                    rows = list(reader)
                self.assertEqual(reader.fieldnames, columns)
                self.assertGreaterEqual(len(rows), minimum_rows)
                self.assertTrue(all(None not in row for row in rows))


EXPECTED = {'camera_catalog.csv': (['camera_id', 'road_id', 'direction', 'km_marker', 'limit_kph', 'map_version', 'valid_from', 'valid_to', 'review_state'], 35), 'trajectory_windows.csv': (['window_id', 'vehicle_id', 'road_id', 'direction', 'started_at', 'sample_rate_hz', 'traffic_state', 'weather'], 48), 'speed_samples.csv': (['window_id', 'offset_m', 'speed_kph', 'strategy_enabled', 'camera_seen', 'map_version'], 192), 'region_plans.csv': (['plan_id', 'region', 'road_id', 'direction', 'version', 'status', 'effective_from', 'receipt_id'], 21)}


if __name__ == "__main__":
    unittest.main()
