"""命令行入口。

子命令：

* ``import``   载入资料 CSV，校验后写出不可变快照。
* ``analyze``  读取 CSV 或快照，输出曝光、效应与区域建议。
* ``plans``    列出计划版本及生效范围。
* ``withdraw`` 计算局部撤回影响（只输出结果，不直接写入输入资料）。
* ``review``   对比更正前后快照，列出受影响的历史路线关联。
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path
from typing import Sequence

from .effects import DEFAULT_MIN_PER_ARM, evaluate_strategy
from .exposure import build_exposures
from .loader import MaterialSchemaError, load_materials, read_snapshot
from .regions import (
    build_ledger,
    recommend_regions,
    withdraw_road_direction,
    withdraw_scope,
)
from .reporting import (
    analysis_report,
    correction_report,
    import_snapshot,
    write_json,
)
from .review import assess_catalog_correction


def _scope_counts(exposure) -> dict[tuple[str, str], dict[str, int]]:
    counts: dict[tuple[str, str], dict[str, int]] = {}
    for item in exposure.exposures:
        key = (item.road_id, item.direction)
        bucket = counts.setdefault(key, {"eligible": 0, "strategy": 0, "control": 0})
        if item.eligible:
            bucket["eligible"] += 1
            if item.arm == "strategy":
                bucket["strategy"] += 1
            elif item.arm == "control":
                bucket["control"] += 1
    return counts


def _load(source: Path):
    if source.is_dir():
        return load_materials(source)
    if source.suffix.lower() == ".json":
        return read_snapshot(source)
    raise MaterialSchemaError("来源必须是 CSV 目录或以 .json 结尾的快照")


def _cmd_import(args: argparse.Namespace) -> int:
    load = load_materials(args.data_dir)
    errors = [issue for issue in load.issues if issue.severity == "error"]
    if errors:
        for issue in errors:
            print(json.dumps(issue.as_dict(), ensure_ascii=False), file=sys.stderr)
        return 2
    target = import_snapshot(load.materials, load, args.output)
    print(json.dumps({"snapshot": str(target), "row_counts": load.row_counts}, ensure_ascii=False))
    return 0


def _build_analysis(source: Path, min_per_arm: int):
    load = _load(source)
    exposure = build_exposures(load.materials)
    effects = evaluate_strategy(load.materials, exposure, min_per_arm=min_per_arm)
    ledger = build_ledger(load.materials)
    counts = _scope_counts(exposure)
    recommendations = recommend_regions(ledger, effects, counts)
    return load, exposure, effects, ledger, recommendations, counts


def _cmd_analyze(args: argparse.Namespace) -> int:
    load, exposure, effects, ledger, recommendations, counts = _build_analysis(
        Path(args.source), args.min_per_arm
    )
    payload = analysis_report(
        load, exposure, effects, ledger, recommendations, scope_counts=counts
    )
    if args.output:
        write_json(args.output, payload)
        print(json.dumps({"report": args.output}, ensure_ascii=False))
    else:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


def _cmd_plans(args: argparse.Namespace) -> int:
    load = _load(Path(args.source))
    ledger = build_ledger(load.materials)
    rows = ledger.effective if args.effective_only else ledger.scopes
    print(json.dumps([item.as_dict() for item in rows], ensure_ascii=False, indent=2))
    return 0


def _cmd_withdraw(args: argparse.Namespace) -> int:
    load = _load(Path(args.source))
    ledger = build_ledger(load.materials)
    effective_from = date.fromisoformat(args.effective_from) if args.effective_from else None
    if args.plan_id:
        result = withdraw_scope(
            ledger,
            args.plan_id,
            withdrawal_plan_id=args.new_plan_id,
            effective_from=effective_from,
            version=args.version,
        )
    elif args.road_id and args.direction:
        result = withdraw_road_direction(
            ledger, args.road_id, args.direction, version=args.version
        )
    else:
        print("需要 --plan-id，或同时提供 --road-id 与 --direction", file=sys.stderr)
        return 2
    payload = result.as_dict()
    if args.output:
        write_json(args.output, payload)
        print(json.dumps({"withdrawal": args.output}, ensure_ascii=False))
    else:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


def _cmd_review(args: argparse.Namespace) -> int:
    before = _load(Path(args.before))
    after = _load(Path(args.after))
    ledger = build_ledger(after.materials)
    impact = assess_catalog_correction(before.materials, after.materials, ledger=ledger)
    payload = correction_report(impact)
    if args.output:
        write_json(args.output, payload)
        print(json.dumps({"review": args.output}, ensure_ascii=False))
    else:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="speed-camera-eval",
        description="测速摄像头减速策略的本地合成资料验证与区域启用评审。",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    import_parser = sub.add_parser("import", help="校验 CSV 并写出快照")
    import_parser.add_argument("data_dir", help="包含四个契约 CSV 的目录")
    import_parser.add_argument("-o", "--output", required=True, help="快照 JSON 路径")
    import_parser.set_defaults(func=_cmd_import)

    analyze = sub.add_parser("analyze", help="生成曝光、效应与区域建议")
    analyze.add_argument("source", help="CSV 目录或快照 JSON")
    analyze.add_argument("-o", "--output", help="报告 JSON 路径；省略则打印到标准输出")
    analyze.add_argument(
        "--min-per-arm",
        type=int,
        default=DEFAULT_MIN_PER_ARM,
        help=(
            "同一条件下策略与对照各需的最少窗口数"
            f"（默认 {DEFAULT_MIN_PER_ARM}）"
        ),
    )
    analyze.set_defaults(func=_cmd_analyze)

    plans = sub.add_parser("plans", help="列出生效范围解析结果")
    plans.add_argument("source", help="CSV 目录或快照 JSON")
    plans.add_argument("--effective-only", action="store_true", help="只列当前生效范围")
    plans.set_defaults(func=_cmd_plans)

    withdraw = sub.add_parser("withdraw", help="计算局部撤回影响（不修改输入）")
    withdraw.add_argument("source", help="CSV 目录或快照 JSON")
    withdraw.add_argument("--plan-id", help="要撤回的计划标识")
    withdraw.add_argument("--road-id", help="按道路撤回")
    withdraw.add_argument("--direction", help="按方向撤回，需与 --road-id 同时给出")
    withdraw.add_argument("--version", help="限定计划版本")
    withdraw.add_argument("--new-plan-id", help="建议的新撤回版本计划标识")
    withdraw.add_argument("--effective-from", help="撤回生效日 YYYY-MM-DD")
    withdraw.add_argument("-o", "--output", help="结果 JSON 路径")
    withdraw.set_defaults(func=_cmd_withdraw)

    review = sub.add_parser("review", help="对比更正前后资料，列出受影响关联")
    review.add_argument("before", help="更正前 CSV 目录或快照")
    review.add_argument("after", help="更正后 CSV 目录或快照")
    review.add_argument("-o", "--output", help="结果 JSON 路径")
    review.set_defaults(func=_cmd_review)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except (MaterialSchemaError, KeyError) as exc:
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
