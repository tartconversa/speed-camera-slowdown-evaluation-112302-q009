"""报告导出：稳定 JSON 结构，供调度与运营复核使用。"""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

from .effects import EffectReport
from .exposure import ExposureReport
from .loader import LoadResult
from .models import Materials
from .regions import PlanLedger, RegionRecommendation
from .review import CorrectionImpact


def _default(value: Any) -> Any:
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    raise TypeError(f"不可序列化类型：{type(value)!r}")


def _dump(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True, default=_default)


def write_json(path: str | Path, payload: Any) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(_dump(payload) + "\n", encoding="utf-8")
    return target


def import_snapshot(materials: Materials, load: LoadResult, path: str | Path) -> Path:
    payload = {
        "format": "speed-camera-materials/v1",
        "imported_at": datetime.now(tz=timezone.utc).isoformat(),
        "row_counts": load.row_counts,
        "issues": [issue.as_dict() for issue in load.issues],
        "cameras": [
            {
                "camera_id": camera.camera_id,
                "road_id": camera.road_id,
                "direction": camera.direction,
                "km_marker": camera.km_marker,
                "limit_kph": camera.limit_kph,
                "map_version": camera.map_version,
                "valid_from": None if camera.valid_from is None else camera.valid_from.isoformat(),
                "valid_to": None if camera.valid_to is None else camera.valid_to.isoformat(),
                "review_state": str(camera.review_state),
                "source_row": camera.source_row,
            }
            for camera in materials.cameras
        ],
        "windows": [
            {
                "window_id": window.window_id,
                "vehicle_id": window.vehicle_id,
                "road_id": window.road_id,
                "direction": window.direction,
                "started_at": window.started_at.isoformat(),
                "sample_rate_hz": window.sample_rate_hz,
                "traffic_state": str(window.traffic_state),
                "weather": window.weather,
                "source_row": window.source_row,
            }
            for window in materials.windows
        ],
        "samples": [
            {
                "window_id": sample.window_id,
                "offset_m": sample.offset_m,
                "speed_kph": sample.speed_kph,
                "strategy_enabled": sample.strategy_enabled,
                "camera_seen": sample.camera_seen,
                "map_version": sample.map_version,
                "source_row": sample.source_row,
            }
            for sample in materials.samples
        ],
        "plans": [
            {
                "plan_id": plan.plan_id,
                "region": plan.region,
                "road_id": plan.road_id,
                "direction": plan.direction,
                "version": plan.version,
                "status": str(plan.status),
                "effective_from": plan.effective_from.isoformat(),
                "receipt_id": plan.receipt_id,
                "source_row": plan.source_row,
            }
            for plan in materials.plans
        ],
    }
    return write_json(path, payload)


def analysis_report(
    load: LoadResult,
    exposure: ExposureReport,
    effects: EffectReport,
    ledger: PlanLedger,
    recommendations: tuple[RegionRecommendation, ...],
    *,
    scope_counts: dict[tuple[str, str], dict[str, int]],
) -> dict[str, Any]:
    return {
        "summary": _summary(exposure, effects),
        "data_quality": {
            "row_counts": load.row_counts,
            "issues": [issue.as_dict() for issue in load.issues],
        },
        "exposure": exposure.as_dict(),
        "effects": effects.as_dict(),
        "plans": ledger.as_dict(),
        "recommendations": [item.as_dict() for item in recommendations],
        "coverage_gaps": coverage_gaps(exposure),
        "scope_counts": _scope_counts(scope_counts),
        "disclaimer": (
            "本报告是对公开功能说明的本地合成资料验证设想，不表示新闻已报道"
            "具体交通事件，也不构成真实道路或车辆结论。"
        ),
    }


def _summary(exposure: ExposureReport, effects: EffectReport) -> dict[str, Any]:
    return {
        "windows_total": len(exposure.exposures),
        "windows_eligible": len(exposure.eligible),
        "window_arms": {
            "strategy": sum(1 for item in exposure.eligible if item.arm == "strategy"),
            "control": sum(1 for item in exposure.eligible if item.arm == "control"),
        },
        "exposure_status_counts": exposure.counts,
        "scopes_with_eligible_windows": sum(
            1 for scope in exposure.scope_coverage if scope.eligible_windows
        ),
        "scopes_with_both_arms": sum(
            1
            for scope in exposure.scope_coverage
            if scope.strategy_windows and scope.control_windows
        ),
        "estimated_scopes": sum(1 for item in effects.scopes if str(item.verdict) == "estimated"),
        "inconclusive_scopes": sum(
            1 for item in effects.scopes if str(item.verdict) == "inconclusive"
        ),
        "insufficient_control_scopes": sum(
            1 for item in effects.scopes if str(item.verdict) == "insufficient_control"
        ),
    }


def coverage_gaps(exposure: ExposureReport) -> list[dict[str, Any]]:
    """列出无法支撑同范围对照的范围及原因，避免把条目数当成有效暴露。"""

    gaps: list[dict[str, Any]] = []
    for scope in exposure.scope_coverage:
        reasons: list[str] = []
        if scope.windows == 0:
            reasons.append("目录条目没有对应的轨迹窗口。")
        elif scope.eligible_windows == 0:
            reasons.append("没有窗口同时满足确认目录、车辆识别与前后几何。")
        else:
            if scope.strategy_windows == 0:
                reasons.append("缺少策略窗口。")
            if scope.control_windows == 0:
                reasons.append("缺少未启用对照窗口。")
        if not reasons:
            continue
        gaps.append(
            {
                "road_id": scope.road_id,
                "direction": scope.direction,
                "camera_entries": scope.camera_entries,
                "confirmed_camera_entries": scope.confirmed_camera_entries,
                "windows": scope.windows,
                "eligible_windows": scope.eligible_windows,
                "reasons": reasons,
            }
        )
    return gaps


def _scope_counts(counts: dict[tuple[str, str], dict[str, int]]) -> list[dict[str, Any]]:
    return [
        {"road_id": road, "direction": direction, **dict(values)}
        for (road, direction), values in sorted(counts.items())
    ]


def correction_report(impact: CorrectionImpact) -> dict[str, Any]:
    return {"correction_impact": impact.as_dict()}
