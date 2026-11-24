"""目录更正与范围复审。

运营现实：摄像头目录会因方向录错、道路层级错误、撤下或限速变化而被更正。
历史行程不能重写，但必须能回答“哪些历史路线关联会受影响”。本模块以更正
前后两份快照对比，给出：

* 因更正而失去 / 新增 / 改变的窗口关联（``lost`` 需要优先复核）；
* 仍处于生效范围、但输入已变化的计划（应重新审查）；
* 历史行程清单，提示保持原始地图和策略状态。

未生效范围（``proposed``）本身就不会被当成已验证；若目录条目被撤回或限速
变化，相关生效范围进入 ``recheck_scopes``，不能沿用更正前的结论。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from .exposure import WindowExposure, build_exposures
from .models import Materials, ReviewState
from .regions import PlanLedger


class AssociationChange(StrEnum):
    LOST = "lost"
    GAINED = "gained"
    CHANGED = "changed"
    UNCHANGED = "unchanged"


@dataclass(frozen=True, slots=True)
class WindowAssociationShift:
    window_id: str
    road_id: str
    direction: str
    before_window_status: str
    after_window_status: str
    before_camera_ids: tuple[str, ...]
    after_camera_ids: tuple[str, ...]
    before_limits: tuple[int, ...]
    after_limits: tuple[int, ...]
    change: AssociationChange
    note: str

    def as_dict(self) -> dict[str, object]:
        return {
            "window_id": self.window_id,
            "road_id": self.road_id,
            "direction": self.direction,
            "before_window_status": self.before_window_status,
            "after_window_status": self.after_window_status,
            "before_camera_ids": list(self.before_camera_ids),
            "after_camera_ids": list(self.after_camera_ids),
            "before_limits": list(self.before_limits),
            "after_limits": list(self.after_limits),
            "change": str(self.change),
            "note": self.note,
        }


@dataclass(frozen=True, slots=True)
class RecheckScope:
    road_id: str
    direction: str
    plan_ids: tuple[str, ...]
    reasons: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "road_id": self.road_id,
            "direction": self.direction,
            "plan_ids": list(self.plan_ids),
            "reasons": list(self.reasons),
        }


@dataclass(frozen=True, slots=True)
class CorrectionImpact:
    changed_windows: tuple[WindowAssociationShift, ...]
    recheck_scopes: tuple[RecheckScope, ...]
    unaffected_windows: int
    historical_windows: tuple[dict[str, object], ...]
    note: str

    def as_dict(self) -> dict[str, object]:
        return {
            "changed_windows": [item.as_dict() for item in self.changed_windows],
            "recheck_scopes": [item.as_dict() for item in self.recheck_scopes],
            "unaffected_windows": self.unaffected_windows,
            "historical_windows": list(self.historical_windows),
            "note": self.note,
        }


def assess_catalog_correction(
    before: Materials,
    after: Materials,
    *,
    ledger: PlanLedger | None = None,
) -> CorrectionImpact:
    """对比两次资料快照，报告目录更正对历史路线关联的影响。

    窗口集合以更正前的轨迹为准；更正后新增的窗口不属于“历史路线关联”变更。
    若传入更正后的计划账本，只把当前生效范围列入重新审查清单。
    """

    before_exposure = build_exposures(before)
    after_exposure = build_exposures(after)
    after_by_id = {item.window_id: item for item in after_exposure.exposures}
    before_windows = {window.window_id: window for window in before.windows}

    shifts: list[WindowAssociationShift] = []
    for old in before_exposure.exposures:
        new = after_by_id.get(old.window_id)
        if new is None:
            shifts.append(
                WindowAssociationShift(
                    window_id=old.window_id,
                    road_id=old.road_id,
                    direction=old.direction,
                    before_window_status=str(old.status),
                    after_window_status="missing_window",
                    before_camera_ids=old.camera_ids,
                    after_camera_ids=(),
                    before_limits=old.limit_kph_values,
                    after_limits=(),
                    change=AssociationChange.LOST,
                    note="更正后的资料不再包含该窗口。",
                )
            )
            continue
        change = _classify(old, new)
        shifts.append(_shift(old, new, change, _note_for(new, change)))

    changed = [item for item in shifts if item.change is not AssociationChange.UNCHANGED]
    unaffected = len(shifts) - len(changed)

    effective = (
        ledger.effective
        if ledger is not None
        else ()
    )
    effective_by_scope: dict[tuple[str, str], list[str]] = {}
    for plan in effective:
        effective_by_scope.setdefault(plan.key, []).append(plan.plan_id)

    reasons_by_scope: dict[tuple[str, str], list[str]] = {}
    for shift in changed:
        scope = (shift.road_id, shift.direction)
        if scope not in effective_by_scope:
            continue
        reasons_by_scope.setdefault(scope, []).append(
            f"窗口 {shift.window_id} 的关联由 {shift.before_window_status} "
            f"变为 {shift.after_window_status}（{shift.change}）。"
        )

    for scope, reason in _catalog_change_reasons(before, after):
        if scope in effective_by_scope:
            reasons_by_scope.setdefault(scope, []).append(reason)

    recheck: list[RecheckScope] = []
    for scope, reasons in sorted(reasons_by_scope.items()):
        recheck.append(
            RecheckScope(
                road_id=scope[0],
                direction=scope[1],
                plan_ids=tuple(sorted(set(effective_by_scope[scope]))),
                reasons=tuple(reasons),
            )
        )

    # 被撤回/改限速的目录条目若属于非生效范围，也要提示“不得再作为已验证”。
    for scope, reason in _catalog_change_reasons(before, after):
        if scope in effective_by_scope or any(
            item.road_id == scope[0] and item.direction == scope[1] for item in recheck
        ):
            continue
        state = _scope_state(ledger, scope)
        recheck.append(
            RecheckScope(
                road_id=scope[0],
                direction=scope[1],
                plan_ids=(),
                reasons=(
                    reason,
                    f"该范围计划状态为 {state}；未生效范围不应据此扩大。",
                ),
            )
        )

    historical = tuple(
        {
            "window_id": window.window_id,
            "vehicle_id": window.vehicle_id,
            "road_id": window.road_id,
            "direction": window.direction,
            "started_at": window.started_at.isoformat(),
            "note": "历史行程保持原始地图版本与策略状态，不随目录更正重写。",
        }
        for window in before_windows.values()
    )

    return CorrectionImpact(
        changed_windows=tuple(changed),
        recheck_scopes=tuple(sorted(recheck, key=lambda item: (item.road_id, item.direction))),
        unaffected_windows=unaffected,
        historical_windows=historical,
        note=(
            "更正只影响关联解释，不改变已记录行程的速度与策略状态；"
            "受影响范围应重新审查后再决定是否继续。"
        ),
    )


def _classify(old: WindowExposure, new: WindowExposure) -> AssociationChange:
    if old.eligible and not new.eligible:
        return AssociationChange.LOST
    if new.eligible and not old.eligible:
        return AssociationChange.GAINED
    if old.status is not new.status:
        return AssociationChange.CHANGED
    if (old.camera_ids, old.limit_kph_values) != (new.camera_ids, new.limit_kph_values):
        return AssociationChange.CHANGED
    return AssociationChange.UNCHANGED


def _shift(
    old: WindowExposure,
    new: WindowExposure,
    change: AssociationChange,
    note: str,
) -> WindowAssociationShift:
    return WindowAssociationShift(
        window_id=old.window_id,
        road_id=old.road_id,
        direction=old.direction,
        before_window_status=str(old.status),
        after_window_status=str(new.status),
        before_camera_ids=old.camera_ids,
        after_camera_ids=new.camera_ids,
        before_limits=old.limit_kph_values,
        after_limits=new.limit_kph_values,
        change=change,
        note=note,
    )


def _note_for(new: WindowExposure, change: AssociationChange) -> str:
    if change is AssociationChange.LOST:
        return f"更正后窗口从可比较变为 {new.status}：{new.note}"
    if change is AssociationChange.GAINED:
        return f"更正后窗口变为可比较：{new.note}"
    if change is AssociationChange.CHANGED:
        return "关联的目录条目或限速发生变化。"
    return ""


def _catalog_change_reasons(
    before: Materials, after: Materials
) -> list[tuple[tuple[str, str], str]]:
    after_ids = {camera.camera_id: camera for camera in after.cameras}
    reasons: list[tuple[tuple[str, str], str]] = []
    for camera in before.cameras:
        updated = after_ids.get(camera.camera_id)
        scope = (camera.road_id, camera.direction)
        if updated is None:
            reasons.append((scope, f"目录条目 {camera.camera_id} 已被移除。"))
        elif (
            updated.review_state is ReviewState.WITHDRAWN
            and camera.review_state is not ReviewState.WITHDRAWN
        ):
            reasons.append((scope, f"目录条目 {camera.camera_id} 已撤回。"))
        elif updated.limit_kph != camera.limit_kph:
            reasons.append(
                (
                    scope,
                    f"目录条目 {camera.camera_id} 限速由 {camera.limit_kph} "
                    f"变为 {updated.limit_kph}。",
                )
            )
        elif updated.direction != camera.direction:
            reasons.append(
                (
                    scope,
                    f"目录条目 {camera.camera_id} 方向由 {camera.direction} "
                    f"更正为 {updated.direction}。",
                )
            )
    return reasons


def _scope_state(ledger: PlanLedger | None, scope: tuple[str, str]) -> str:
    if ledger is None:
        return "unknown"
    states = sorted(
        {str(item.state) for item in ledger.scopes if item.key == scope}
    )
    return ",".join(states) if states else "no_plan"
