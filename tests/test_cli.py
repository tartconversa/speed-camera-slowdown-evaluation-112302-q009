from __future__ import annotations

import json
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

from speed_camera_slowdown_evaluation.cli import main

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"


def run_cli(*argv: str) -> tuple[int, str]:
    buffer = StringIO()
    with redirect_stdout(buffer):
        code = main(list(argv))
    return code, buffer.getvalue()


class CliTests(unittest.TestCase):
    def test_import_analyze_round_trip_through_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            snapshot = Path(tmp) / "snapshot.json"
            code, _ = run_cli("import", str(DATA), "-o", str(snapshot))
            self.assertEqual(code, 0)
            self.assertTrue(snapshot.is_file())

            report = Path(tmp) / "report.json"
            code, _ = run_cli("analyze", str(snapshot), "-o", str(report))
            self.assertEqual(code, 0)
            payload = json.loads(report.read_text(encoding="utf-8"))
        self.assertEqual(payload["summary"]["windows_total"], 48)
        self.assertEqual(payload["summary"]["windows_eligible"], 16)
        self.assertEqual(payload["effects"]["overall"]["verdict"], "insufficient_control")

    def test_analyze_prints_without_output(self):
        code, output = run_cli("analyze", str(DATA))
        self.assertEqual(code, 0)
        payload = json.loads(output)
        self.assertIn("recommendations", payload)

    def test_plans_effective_only(self):
        code, output = run_cli("plans", str(DATA), "--effective-only")
        self.assertEqual(code, 0)
        rows = json.loads(output)
        self.assertTrue(rows)
        self.assertTrue(all(row["state"] == "effective" for row in rows))

    def test_withdraw_road_direction_keeps_opposite(self):
        code, output = run_cli(
            "withdraw", str(DATA), "--road-id", "RD-01", "--direction", "S"
        )
        self.assertEqual(code, 0)
        payload = json.loads(output)
        self.assertIn("P-13", payload["affected_plan_ids"])
        retained = {item["road_id"] + "/" + item["direction"] for item in payload["retained"]}
        self.assertNotIn("RD-01/S", retained)

    def test_withdraw_requires_target(self):
        code, _ = run_cli("withdraw", str(DATA))
        self.assertEqual(code, 2)

    def test_unknown_plan_returns_error_code(self):
        code, _ = run_cli("withdraw", str(DATA), "--plan-id", "P-999")
        self.assertEqual(code, 2)

    def test_review_reports_direction_fix(self):
        with tempfile.TemporaryDirectory() as tmp:
            import shutil

            copy = Path(tmp) / "data"
            shutil.copytree(DATA, copy)
            catalog = copy / "camera_catalog.csv"
            lines = catalog.read_text(encoding="utf-8-sig").splitlines()
            # 把 RD-03/W 全部确认条目的方向改成 E，模拟方向录错被更正。
            for index in range(1, len(lines)):
                fields = lines[index].split(",")
                if fields[1] == "RD-03" and fields[2] == "W":
                    fields[2] = "E"
                    lines[index] = ",".join(fields)
            catalog.write_text("﻿" + "\n".join(lines) + "\n", encoding="utf-8")

            code, output = run_cli("review", str(DATA), str(copy))
            self.assertEqual(code, 0)
            payload = json.loads(output)["correction_impact"]
        lost = [
            item for item in payload["changed_windows"]
            if item["change"] == "lost"
        ]
        self.assertTrue(lost)
        self.assertEqual({item["window_id"] for item in lost}, {"W-003", "W-015", "W-027", "W-039"})
        self.assertTrue(all(item["road_id"] == "RD-03" for item in lost))
        recheck = {item["road_id"] + "/" + item["direction"]: item for item in payload["recheck_scopes"]}
        self.assertIn("RD-03/W", recheck)


if __name__ == "__main__":
    unittest.main()
