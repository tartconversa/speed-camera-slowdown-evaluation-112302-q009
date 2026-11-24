"""严格 CSV 载入与资料质量诊断。

载入阶段只做格式、类型、主键与基本引用完整性检查。业务语义（例如
“已确认目录条目才可形成暴露”“撤回不等于零事件”）由后续模块处理。
任何被跳过的行都会留下 Issue，而不是静默计数为零。
"""

from __future__ import annotations

import csv
import math
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .models import (
    Camera,
    Materials,
    PlanStatus,
    RegionPlan,
    ReviewState,
    SpeedSample,
    TrafficState,
    TrajectoryWindow,
)

HEADERS: dict[str, tuple[str, ...]] = {
    "camera_catalog.csv": (
        "camera_id",
        "road_id",
        "direction",
        "km_marker",
        "limit_kph",
        "map_version",
        "valid_from",
        "valid_to",
        "review_state",
    ),
    "trajectory_windows.csv": (
        "window_id",
        "vehicle_id",
        "road_id",
        "direction",
        "started_at",
        "sample_rate_hz",
        "traffic_state",
        "weather",
    ),
    "speed_samples.csv": (
        "window_id",
        "offset_m",
        "speed_kph",
        "strategy_enabled",
        "camera_seen",
        "map_version",
    ),
    "region_plans.csv": (
        "plan_id",
        "region",
        "road_id",
        "direction",
        "version",
        "status",
        "effective_from",
        "receipt_id",
    ),
}

DIRECTIONS = frozenset({"N", "S", "E", "W"})
YES_NO = frozenset({"yes", "no"})


class MaterialSchemaError(ValueError):
    """表头或文件级结构不合约。"""


class RowError(ValueError):
    """单行字段无法解析。"""


@dataclass(frozen=True, slots=True)
class Issue:
    code: str
    message: str
    severity: str = "error"
    file: str = ""
    row: int | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "severity": self.severity,
            "code": self.code,
            "message": self.message,
            "file": self.file,
            "row": self.row,
        }


@dataclass(frozen=True, slots=True)
class LoadResult:
    materials: Materials
    issues: tuple[Issue, ...]
    row_counts: dict[str, int] = field(default_factory=dict)

    def issues_by_severity(self, severity: str) -> tuple[Issue, ...]:
        return tuple(issue for issue in self.issues if issue.severity == severity)


def load_materials(data_dir: str | Path) -> LoadResult:
    """从目录读取四个契约 CSV，返回记录与全部诊断。"""

    root = Path(data_dir)
    issues: list[Issue] = []
    row_counts: dict[str, int] = {}

    camera_rows = _read_rows(root, "camera_catalog.csv", issues, row_counts)
    window_rows = _read_rows(root, "trajectory_windows.csv", issues, row_counts)
    sample_rows = _read_rows(root, "speed_samples.csv", issues, row_counts)
    plan_rows = _read_rows(root, "region_plans.csv", issues, row_counts)

    cameras: list[Camera] = []
    seen_cameras: set[str] = set()
    for line, row in camera_rows:
        try:
            camera = _parse_camera(row, line)
        except RowError as exc:
            issues.append(_row_issue("camera_parse_error", exc, "camera_catalog.csv", line))
            continue
        if camera.camera_id in seen_cameras:
            issues.append(
                Issue(
                    "duplicate_camera_id",
                    f"摄像头标识 {camera.camera_id} 重复，保留首次出现记录。",
                    file="camera_catalog.csv",
                    row=line,
                )
            )
            continue
        seen_cameras.add(camera.camera_id)
        cameras.append(camera)

    windows: list[TrajectoryWindow] = []
    seen_windows: set[str] = set()
    for line, row in window_rows:
        try:
            window = _parse_window(row, line)
        except RowError as exc:
            issues.append(_row_issue("window_parse_error", exc, "trajectory_windows.csv", line))
            continue
        if window.window_id in seen_windows:
            issues.append(
                Issue(
                    "duplicate_window_id",
                    f"窗口标识 {window.window_id} 重复，保留首次出现记录。",
                    file="trajectory_windows.csv",
                    row=line,
                )
            )
            continue
        seen_windows.add(window.window_id)
        windows.append(window)

    samples: list[SpeedSample] = []
    seen_sample_keys: set[tuple[str, float]] = set()
    for line, row in sample_rows:
        try:
            sample = _parse_sample(row, line)
        except RowError as exc:
            issues.append(_row_issue("sample_parse_error", exc, "speed_samples.csv", line))
            continue
        if sample.window_id not in seen_windows:
            issues.append(
                Issue(
                    "orphan_sample",
                    f"速度样本引用了不存在的窗口 {sample.window_id}，未纳入分析。",
                    file="speed_samples.csv",
                    row=line,
                )
            )
            continue
        key = (sample.window_id, sample.offset_m)
        if key in seen_sample_keys:
            issues.append(
                Issue(
                    "duplicate_sample_offset",
                    f"窗口 {sample.window_id} 在偏移 {sample.offset_m:g} m 处有重复样本，保留首次记录。",
                    severity="warning",
                    file="speed_samples.csv",
                    row=line,
                )
            )
            continue
        seen_sample_keys.add(key)
        samples.append(sample)

    plans: list[RegionPlan] = []
    seen_plans: set[str] = set()
    for line, row in plan_rows:
        try:
            plan = _parse_plan(row, line)
        except RowError as exc:
            issues.append(_row_issue("plan_parse_error", exc, "region_plans.csv", line))
            continue
        if plan.plan_id in seen_plans:
            issues.append(
                Issue(
                    "duplicate_plan_id",
                    f"计划标识 {plan.plan_id} 重复，保留首次出现记录。",
                    file="region_plans.csv",
                    row=line,
                )
            )
            continue
        seen_plans.add(plan.plan_id)
        plans.append(plan)

    materials = Materials(
        cameras=tuple(cameras),
        windows=tuple(windows),
        samples=tuple(samples),
        plans=tuple(plans),
    )
    issues.extend(_cross_checks(materials))
    return LoadResult(
        materials=materials,
        issues=tuple(issues),
        row_counts=row_counts,
    )


def _read_rows(
    root: Path,
    filename: str,
    issues: list[Issue],
    row_counts: dict[str, int],
) -> list[tuple[int, dict[str, str]]]:
    path = root / filename
    if not path.is_file():
        raise MaterialSchemaError(f"缺少资料文件：{path}")
    try:
        with path.open(encoding="utf-8-sig", newline="") as stream:
            reader = csv.DictReader(stream)
            if reader.fieldnames != list(HEADERS[filename]):
                raise MaterialSchemaError(
                    f"{filename} 表头不合约：期望 {list(HEADERS[filename])}，"
                    f"实际 {reader.fieldnames}"
                )
            rows: list[tuple[int, dict[str, str]]] = []
            for line, row in enumerate(reader, start=2):
                if None in row:
                    issues.append(
                        Issue(
                            "extra_columns",
                            f"{filename} 第 {line} 行列数超过表头，已跳过该行。",
                            file=filename,
                            row=line,
                        )
                    )
                    continue
                if any(value is None for value in row.values()):
                    issues.append(
                        Issue(
                            "short_row",
                            f"{filename} 第 {line} 行字段数不足，已跳过该行。",
                            file=filename,
                            row=line,
                        )
                    )
                    continue
                rows.append((line, row))
    except UnicodeDecodeError as exc:
        raise MaterialSchemaError(f"{filename} 不是有效 UTF-8：{exc}") from exc
    row_counts[filename] = len(rows)
    return rows


def _require(row: dict[str, str], key: str) -> str:
    value = row.get(key, "").strip()
    if not value:
        raise RowError(f"字段 {key} 为空")
    return value


def _parse_direction(value: str) -> str:
    direction = value.strip().upper()
    if direction not in DIRECTIONS:
        raise RowError(f"方向 {value!r} 不是 N/S/E/W")
    return direction


def _parse_float(value: str, key: str) -> float:
    try:
        number = float(value)
    except ValueError as exc:
        raise RowError(f"字段 {key}={value!r} 不是数字") from exc
    if not math.isfinite(number):
        raise RowError(f"字段 {key}={value!r} 不是有限数字")
    return number


def _parse_int(value: str, key: str) -> int:
    try:
        return int(value)
    except ValueError as exc:
        raise RowError(f"字段 {key}={value!r} 不是整数") from exc


def _parse_date(value: str, key: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise RowError(f"字段 {key}={value!r} 不是 ISO 日期") from exc


def _parse_optional_date(value: str, key: str) -> date | None:
    text = value.strip()
    return None if not text else _parse_date(text, key)


def _parse_datetime(value: str) -> datetime:
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise RowError(f"字段 started_at={value!r} 不是 ISO 时间") from exc
    if parsed.tzinfo is None:
        raise RowError("字段 started_at 必须带时区（资料契约为 UTC）")
    return parsed.astimezone(timezone.utc)


def _parse_bool(value: str, key: str) -> bool:
    text = value.strip().lower()
    if text not in YES_NO:
        raise RowError(f"字段 {key}={value!r} 必须是 yes/no")
    return text == "yes"


def _parse_camera(row: dict[str, str], line: int) -> Camera:
    camera_id = _require(row, "camera_id")
    review_state = _require(row, "review_state")
    try:
        state = ReviewState(review_state)
    except ValueError as exc:
        raise RowError(f"字段 review_state={review_state!r} 未定义") from exc
    valid_from = _parse_optional_date(row.get("valid_from", ""), "valid_from")
    valid_to = _parse_optional_date(row.get("valid_to", ""), "valid_to")
    if valid_from is not None and valid_to is not None and valid_to <= valid_from:
        raise RowError("有效区间 valid_to 必须晚于 valid_from")
    limit_kph = _parse_int(_require(row, "limit_kph"), "limit_kph")
    if limit_kph <= 0:
        raise RowError("字段 limit_kph 必须为正数")
    km_marker = _parse_float(_require(row, "km_marker"), "km_marker")
    if km_marker < 0:
        raise RowError("字段 km_marker 不能为负")
    return Camera(
        camera_id=camera_id,
        road_id=_require(row, "road_id"),
        direction=_parse_direction(_require(row, "direction")),
        km_marker=km_marker,
        limit_kph=limit_kph,
        map_version=_require(row, "map_version"),
        valid_from=valid_from,
        valid_to=valid_to,
        review_state=state,
        source_row=line,
    )


def _parse_window(row: dict[str, str], line: int) -> TrajectoryWindow:
    traffic = _require(row, "traffic_state").lower()
    try:
        traffic_state = TrafficState(traffic)
    except ValueError as exc:
        raise RowError(f"字段 traffic_state={traffic!r} 未定义") from exc
    rate = _parse_float(_require(row, "sample_rate_hz"), "sample_rate_hz")
    if rate <= 0:
        raise RowError("字段 sample_rate_hz 必须为正数")
    return TrajectoryWindow(
        window_id=_require(row, "window_id"),
        vehicle_id=_require(row, "vehicle_id"),
        road_id=_require(row, "road_id"),
        direction=_parse_direction(_require(row, "direction")),
        started_at=_parse_datetime(_require(row, "started_at")),
        sample_rate_hz=rate,
        traffic_state=traffic_state,
        weather=_require(row, "weather").strip().lower(),
        source_row=line,
    )


def _parse_sample(row: dict[str, str], line: int) -> SpeedSample:
    offset = _parse_float(_require(row, "offset_m"), "offset_m")
    if offset < 0:
        raise RowError("字段 offset_m 不能为负")
    speed = _parse_float(_require(row, "speed_kph"), "speed_kph")
    if speed < 0:
        raise RowError("字段 speed_kph 不能为负")
    return SpeedSample(
        window_id=_require(row, "window_id"),
        offset_m=offset,
        speed_kph=speed,
        strategy_enabled=_parse_bool(_require(row, "strategy_enabled"), "strategy_enabled"),
        camera_seen=_parse_bool(_require(row, "camera_seen"), "camera_seen"),
        map_version=_require(row, "map_version"),
        source_row=line,
    )


def _parse_plan(row: dict[str, str], line: int) -> RegionPlan:
    status_text = _require(row, "status").lower()
    try:
        status = PlanStatus(status_text)
    except ValueError as exc:
        raise RowError(f"字段 status={status_text!r} 未定义") from exc
    return RegionPlan(
        plan_id=_require(row, "plan_id"),
        region=_require(row, "region"),
        road_id=_require(row, "road_id"),
        direction=_parse_direction(_require(row, "direction")),
        version=_require(row, "version"),
        status=status,
        effective_from=_parse_date(_require(row, "effective_from"), "effective_from"),
        receipt_id=_require(row, "receipt_id"),
        source_row=line,
    )


def _row_issue(code: str, error: RowError, filename: str, line: int) -> Issue:
    return Issue(code, str(error), file=filename, row=line)


def _cross_checks(materials: Materials) -> Iterable[Issue]:
    samples_by_window: dict[str, list[SpeedSample]] = {}
    for sample in materials.samples:
        samples_by_window.setdefault(sample.window_id, []).append(sample)

    window_scopes = {(window.road_id, window.direction) for window in materials.windows}
    window_roads = {window.road_id for window in materials.windows}

    for window in materials.windows:
        samples = samples_by_window.get(window.window_id, [])
        if not samples:
            yield Issue(
                "window_without_samples",
                f"窗口 {window.window_id} 没有速度样本，不能参与估计。",
                severity="warning",
                file="trajectory_windows.csv",
                row=window.source_row,
            )
            continue
        map_versions = {sample.map_version for sample in samples}
        if len(map_versions) > 1:
            yield Issue(
                "window_map_version_conflict",
                f"窗口 {window.window_id} 的样本包含多个地图版本 {sorted(map_versions)}；"
                "该窗口不计为有效暴露。",
                severity="warning",
                file="speed_samples.csv",
            )
        strategies = {sample.strategy_enabled for sample in samples}
        if len(strategies) > 1:
            yield Issue(
                "mixed_strategy_state",
                f"窗口 {window.window_id} 内策略启用状态不一致；"
                "该窗口不用于策略效果比较。",
                severity="warning",
                file="speed_samples.csv",
            )
        for previous, current in zip(samples, samples[1:]):
            if current.offset_m < previous.offset_m:
                yield Issue(
                    "out_of_order_offsets",
                    f"窗口 {window.window_id} 的样本偏移不是升序，分析时会重新排序。",
                    severity="warning",
                    file="speed_samples.csv",
                    row=current.source_row,
                )
                break

    for camera in materials.cameras:
        if window_roads and camera.road_id not in window_roads:
            yield Issue(
                "camera_road_without_trajectory",
                f"摄像头 {camera.camera_id} 的道路 {camera.road_id} 没有任何轨迹窗口；"
                "目录条目不能视为暴露。",
                severity="warning",
                file="camera_catalog.csv",
                row=camera.source_row,
            )
        elif (camera.road_id, camera.direction) not in window_scopes:
            yield Issue(
                "camera_direction_without_trajectory",
                f"摄像头 {camera.camera_id} 的方向组合 "
                f"{camera.road_id}/{camera.direction} 没有轨迹窗口；"
                "该条目不能形成暴露，也不代表方向记录一定正确。",
                severity="warning",
                file="camera_catalog.csv",
                row=camera.source_row,
            )

    for plan in materials.plans:
        if plan.road_id not in window_roads:
            yield Issue(
                "plan_road_without_trajectory",
                f"区域计划 {plan.plan_id} 的道路 {plan.road_id} 没有轨迹窗口，"
                "无法验证其行为。",
                severity="warning",
                file="region_plans.csv",
                row=plan.source_row,
            )
        elif (plan.road_id, plan.direction) not in window_scopes:
            yield Issue(
                "plan_direction_without_trajectory",
                f"区域计划 {plan.plan_id} 的方向组合 "
                f"{plan.road_id}/{plan.direction} 没有轨迹窗口。",
                severity="warning",
                file="region_plans.csv",
                row=plan.source_row,
            )

    by_scope: dict[tuple[str, str, str], list[RegionPlan]] = {}
    for plan in materials.plans:
        by_scope.setdefault(plan.key, []).append(plan)
    for key, plans in by_scope.items():
        admitted = [p for p in plans if p.is_effective]
        if len(admitted) > 1:
            dates = sorted(p.effective_from for p in admitted)
            yield Issue(
                "duplicate_effective_plan_scope",
                f"道路 {key[0]}/{key[1]} 版本 {key[2]} 有多条已接受/部分生效记录 "
                f"{[p.plan_id for p in admitted]}，生效日 {dates}；"
                "分析按最新生效记录解释，原记录保持不可变。",
                severity="warning",
                file="region_plans.csv",
            )


def read_snapshot(path: str | Path) -> LoadResult:
    """从 ``import`` 输出的 JSON 快照重新载入资料。

    快照只保存已通过校验的规范记录，用于把一次分析固定到不可变输入。
    """

    import json

    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if payload.get("format") != "speed-camera-materials/v1":
        raise MaterialSchemaError("快照 format 不是 speed-camera-materials/v1")
    materials = Materials(
        cameras=tuple(_camera_from_snapshot(item) for item in payload["cameras"]),
        windows=tuple(_window_from_snapshot(item) for item in payload["windows"]),
        samples=tuple(_sample_from_snapshot(item) for item in payload["samples"]),
        plans=tuple(_plan_from_snapshot(item) for item in payload["plans"]),
    )
    return LoadResult(
        materials=materials,
        issues=tuple(Issue(**item) for item in payload.get("issues", [])),
        row_counts=dict(payload.get("row_counts", {})),
    )


def _camera_from_snapshot(item: dict[str, Any]) -> Camera:
    return Camera(
        camera_id=item["camera_id"],
        road_id=item["road_id"],
        direction=item["direction"],
        km_marker=float(item["km_marker"]),
        limit_kph=int(item["limit_kph"]),
        map_version=item["map_version"],
        valid_from=date.fromisoformat(item["valid_from"]) if item.get("valid_from") else None,
        valid_to=date.fromisoformat(item["valid_to"]) if item.get("valid_to") else None,
        review_state=ReviewState(item["review_state"]),
        source_row=int(item.get("source_row", 0)),
    )


def _window_from_snapshot(item: dict[str, Any]) -> TrajectoryWindow:
    return TrajectoryWindow(
        window_id=item["window_id"],
        vehicle_id=item["vehicle_id"],
        road_id=item["road_id"],
        direction=item["direction"],
        started_at=_parse_datetime(item["started_at"]),
        sample_rate_hz=float(item["sample_rate_hz"]),
        traffic_state=TrafficState(item["traffic_state"]),
        weather=item["weather"],
        source_row=int(item.get("source_row", 0)),
    )


def _sample_from_snapshot(item: dict[str, Any]) -> SpeedSample:
    return SpeedSample(
        window_id=item["window_id"],
        offset_m=float(item["offset_m"]),
        speed_kph=float(item["speed_kph"]),
        strategy_enabled=bool(item["strategy_enabled"]),
        camera_seen=bool(item["camera_seen"]),
        map_version=item["map_version"],
        source_row=int(item.get("source_row", 0)),
    )


def _plan_from_snapshot(item: dict[str, Any]) -> RegionPlan:
    return RegionPlan(
        plan_id=item["plan_id"],
        region=item["region"],
        road_id=item["road_id"],
        direction=item["direction"],
        version=item["version"],
        status=PlanStatus(item["status"]),
        effective_from=date.fromisoformat(item["effective_from"]),
        receipt_id=item["receipt_id"],
        source_row=int(item.get("source_row", 0)),
    )
