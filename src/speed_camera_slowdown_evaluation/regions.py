"""区域启用决定、计划版本与地图变更审查。

计划记录不可变。“撤回”不是原地改字段，而是追加一条 ``status=withdrawn``
的新版本，或在解析时把被撤回覆盖的旧记录标记为不再生效；历史行程仍引用
发生时的地图版本与策略状态。本模块负责：

* 生效范围解析：同一道路/方向按版本演进；未生效范围（``proposed``）单列，
  且必须重新审查后才可能进入生效。
* 决定建议：基于同范围证据给出 ``continue`` / ``limited`` / ``hold``，
  并列出理由与受阻项；不把跨道路敏感性结论当作启用依据。
* 局部撤回：按计划、道路+方向或道路+方向+版本撤下生效范围，返回受影响
  与保留的记录；不删除历史，不误伤同道路对向范围。
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date
from enum import StrEnum

from .effects import EffectReport, Verdict
from .models import Materials, PlanStatus, RegionPlan


class Decision(StrEnum):
    #: 继续采集：已有初步观察但不足以确认，维持现状并补充证据。
    CONTINUE_COLLECT = "continue_collect"
    #: 有限启用：同范围正向且可估计，但只建议小范围启用并继续采集。
    LIMITED = "limited"
    #: 暂缓：缺少可比较证据或对照，暂缓该组道路，不影响历史行程。
    HOLD = "hold"


DECISION_LABELS = {
    Decision.CONTINUE_COLLECT: "继续采集",
    Decision.LIMITED: "有限启用",
    Decision.HOLD: "暂缓",
}


class ScopeState(StrEnum):
    EFFECTIVE = "effective"
    SUPERSEDED = "superseded"
    WITHDRAWN = "withdrawn"
    PROPOSED = "proposed"


@dataclass(frozen=True, slots=True)
class ScopePlan:
    plan_id: str
    region: str
    road_id: str
    direction: str
    version: str
    status: PlanStatus
    effective_from: date
    receipt_id: str
    state: ScopeState
    superseded_by: str | None = None
    withdrawn_by: str | None = None

    @property
    def key(self) -> tuple[str, str]:
        return (self.road_id, self.direction)

    def as_dict(self) -> dict[str, object]:
        return {
            "plan_id": self.plan_id,
            "region": self.region,
            "road_id": self.road_id,
            "direction": self.direction,
            "version": self.version,
            "status": str(self.status),
            "effective_from": self.effective_from.isoformat(),
            "receipt_id": self.receipt_id,
            "state": str(self.state),
            "superseded_by": self.superseded_by,
            "withdrawn_by": self.withdrawn_by,
        }


@dataclass(frozen=True, slots=True)
class RegionRecommendation:
    plan_id: str
    region: str
    road_id: str
    direction: str
    version: str
    scope_state: str
    decision: Decision
    evidence_basis: str
    effect_verdict: str | None
    effect_kph: float | None
    eligible_windows: int
    strategy_windows: int
    control_windows: int
    reasons: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "plan_id": self.plan_id,
            "region": self.region,
            "road_id": self.road_id,
            "direction": self.direction,
            "version": self.version,
            "scope_state": self.scope_state,
            "decision": str(self.decision),
            "evidence_basis": self.evidence_basis,
            "effect_verdict": self.effect_verdict,
            "effect_kph": self.effect_kph,
            "eligible_windows": self.eligible_windows,
            "strategy_windows": self.strategy_windows,
            "control_windows": self.control_windows,
            "reasons": list(self.reasons),
        }


@dataclass(frozen=True, slots=True)
class WithdrawalResult:
    plan_id: str
    withdrawn: ScopePlan | None
    newly_withdrawn: tuple[ScopePlan, ...]
    retained: tuple[ScopePlan, ...]
    already_withdrawn: bool
    affected_plan_ids: tuple[str, ...]
    historical_window_ids: tuple[str, ...]
    note: str

    def as_dict(self) -> dict[str, object]:
        return {
            "plan_id": self.plan_id,
            "withdrawn": None if self.withdrawn is None else self.withdrawn.as_dict(),
            "newly_withdrawn": [item.as_dict() for item in self.newly_withdrawn],
            "retained": [item.as_dict() for item in self.retained],
            "already_withdrawn": self.already_withdrawn,
            "affected_plan_ids": list(self.affected_plan_ids),
            "historical_window_ids": list(self.historical_window_ids),
            "note": self.note,
        }


@dataclass(frozen=True, slots=True)
class PlanLedger:
    scopes: tuple[ScopePlan, ...]
    windows_by_scope: dict[tuple[str, str], tuple[str, ...]]

    @property
    def effective(self) -> tuple[ScopePlan, ...]:
        return tuple(item for item in self.scopes if item.state is ScopeState.EFFECTIVE)

    @property
    def proposed(self) -> tuple[ScopePlan, ...]:
        return tuple(item for item in self.scopes if item.state is ScopeState.PROPOSED)

    def for_road(self, road_id: str) -> tuple[ScopePlan, ...]:
        return tuple(item for item in self.scopes if item.road_id == road_id)

    def find(self, plan_id: str) -> ScopePlan | None:
        for item in self.scopes:
            if item.plan_id == plan_id:
                return item
        return None

    def as_dict(self) -> dict[str, object]:
        return {"scopes": [item.as_dict() for item in self.scopes]}


def build_ledger(materials: Materials) -> PlanLedger:
    """按道路/方向解析计划状态，保留全部历史记录。"""

    grouped: dict[tuple[str, str], list[RegionPlan]] = {}
    for plan in materials.plans:
        grouped.setdefault((plan.road_id, plan.direction), []).append(plan)

    windows_by_scope: dict[tuple[str, str], list[str]] = {}
    for window in materials.windows:
        windows_by_scope.setdefault((window.road_id, window.direction), []).append(window.window_id)

    resolved: list[ScopePlan] = []
    for plans in grouped.values():
        ordered = sorted(plans, key=lambda p: (p.effective_from, p.version, p.plan_id))
        admitted = [p for p in ordered if p.status in (PlanStatus.ACCEPTED, PlanStatus.PARTIAL)]
        withdrawals = [p for p in ordered if p.status is PlanStatus.WITHDRAWN]
        latest_admitted = admitted[-1] if admitted else None
        latest_withdrawal = withdrawals[-1] if withdrawals else None

        for plan in ordered:
            withdrawn_by = None
            superseded_by = None
            if plan.status is PlanStatus.WITHDRAWN:
                state = ScopeState.WITHDRAWN
            elif plan.status is PlanStatus.PROPOSED:
                state = ScopeState.PROPOSED
            elif latest_admitted is not None and plan.plan_id == latest_admitted.plan_id:
                if latest_withdrawal is not None and latest_withdrawal.effective_from >= plan.effective_from:
                    state = ScopeState.WITHDRAWN
                    withdrawn_by = latest_withdrawal.plan_id
                else:
                    state = ScopeState.EFFECTIVE
            else:
                state = ScopeState.SUPERSEDED
                superseded_by = None if latest_admitted is None else latest_admitted.plan_id
            resolved.append(
                ScopePlan(
                    plan_id=plan.plan_id,
                    region=plan.region,
                    road_id=plan.road_id,
                    direction=plan.direction,
                    version=plan.version,
                    status=plan.status,
                    effective_from=plan.effective_from,
                    receipt_id=plan.receipt_id,
                    state=state,
                    superseded_by=superseded_by,
                    withdrawn_by=withdrawn_by,
                )
            )

    # 完全被后续版本取代的生效记录不进入 effective；同一 (道路,方向) 内
    # 正常情况下只可能有一条 effective，这里再做一次防御性收敛。
    effective_by_scope: dict[tuple[str, str], ScopePlan] = {}
    final: list[ScopePlan] = []
    for item in resolved:
        if item.state is ScopeState.EFFECTIVE:
            existing = effective_by_scope.get(item.key)
            if existing is not None:
                newer, older = (
                    (item, existing)
                    if item.effective_from >= existing.effective_from
                    else (existing, item)
                )
                effective_by_scope[item.key] = newer
                older = replace(
                    older,
                    state=ScopeState.SUPERSEDED,
                    superseded_by=newer.plan_id,
                )
                final.append(older)
                continue
            effective_by_scope[item.key] = item
        final.append(item)

    return PlanLedger(
        scopes=tuple(sorted(final, key=lambda entry: entry.plan_id)),
        windows_by_scope={
            key: tuple(value) for key, value in sorted(windows_by_scope.items())
        },
    )


def recommend_regions(
    ledger: PlanLedger,
    effects: EffectReport,
    exposure_counts: dict[tuple[str, str], dict[str, int]],
) -> tuple[RegionRecommendation, ...]:
    """依据同范围证据给出决定；``proposed`` 范围保持暂缓。"""

    estimates = {
        (estimate.road_id, estimate.direction): estimate
        for estimate in effects.scopes
        if str(estimate.verdict) != Verdict.NO_ELIGIBLE_WINDOWS.value
    }
    recommendations: list[RegionRecommendation] = []
    for scope in ledger.scopes:
        if scope.state in (ScopeState.SUPERSEDED, ScopeState.WITHDRAWN):
            continue
        scope_key = (scope.road_id, scope.direction)
        estimate = estimates.get(scope_key)
        counts = exposure_counts.get(scope_key, {})
        eligible = counts.get("eligible", 0)
        strategy = counts.get("strategy", 0)
        control = counts.get("control", 0)
        reasons: list[str] = []

        if scope.state is ScopeState.PROPOSED:
            decision = Decision.HOLD
            basis = "proposed_scope"
            reasons.append("尚未生效的计划，需重新审查后才能进入有限启用或继续采集。")
            if eligible:
                reasons.append(f"当前只有 {eligible} 个可比窗口，不足以支持扩大。")
        elif eligible == 0:
            decision = Decision.HOLD
            basis = "exposure"
            reasons.append("没有有效的可比较窗口，暂缓该组道路的启用。")
        elif control == 0:
            decision = Decision.HOLD
            basis = "exposure"
            reasons.append("只有策略窗口、缺少未启用对照；前后速差不能归因于策略。")
        elif estimate is None:
            decision = Decision.HOLD
            basis = "effects"
            reasons.append("缺少该范围的可信效应估计。")
        else:
            basis = "within_scope_effect"
            if estimate.verdict is Verdict.ESTIMATED:
                if eligible >= 4:
                    decision = Decision.LIMITED
                else:
                    decision = Decision.CONTINUE_COLLECT
                reasons.append(
                    f"同范围配对减速 {estimate.effect_kph:.1f} kph，"
                    f"区间 [{estimate.ci_low_kph:.1f}, {estimate.ci_high_kph:.1f}] 高于 0。"
                )
                if decision is Decision.CONTINUE_COLLECT:
                    reasons.append("有效窗口较少，建议继续采集并保持有限范围。")
            elif estimate.verdict is Verdict.INCONCLUSIVE:
                decision = Decision.CONTINUE_COLLECT
                reasons.append("已有配对但区间跨过 0，无法确认效果，建议继续采集。")
            else:
                decision = Decision.HOLD
                reasons.append(f"效应结论为 {estimate.verdict}，暂缓扩大。")

        recommendations.append(
            RegionRecommendation(
                plan_id=scope.plan_id,
                region=scope.region,
                road_id=scope.road_id,
                direction=scope.direction,
                version=scope.version,
                scope_state=str(scope.state),
                decision=decision,
                evidence_basis=basis,
                effect_verdict=None if estimate is None else str(estimate.verdict),
                effect_kph=None if estimate is None else estimate.effect_kph,
                eligible_windows=eligible,
                strategy_windows=strategy,
                control_windows=control,
                reasons=tuple(reasons),
            )
        )
    return tuple(recommendations)


def withdraw_scope(
    ledger: PlanLedger,
    plan_id: str,
    *,
    withdrawal_plan_id: str | None = None,
    effective_from: date | None = None,
    version: str | None = None,
) -> WithdrawalResult:
    """撤回单条计划；不修改输入账本，只描述操作结果。"""

    target = ledger.find(plan_id)
    if target is None:
        raise KeyError(f"未知计划 {plan_id}")
    if version is not None and target.version != version:
        raise KeyError(
            f"计划 {plan_id} 的版本是 {target.version}，与请求的 {version} 不一致"
        )

    affected = tuple(
        item
        for item in ledger.effective
        if item.plan_id == target.plan_id
    )
    retained = tuple(
        item for item in ledger.effective if item.plan_id != target.plan_id
    )
    historical = ledger.windows_by_scope.get(target.key, ())

    if not affected:
        return WithdrawalResult(
            plan_id=plan_id,
            withdrawn=target,
            newly_withdrawn=(),
            retained=retained,
            already_withdrawn=target.state is ScopeState.WITHDRAWN,
            affected_plan_ids=(),
            historical_window_ids=historical,
            note=(
                "该计划已是撤回状态，重复操作不改变范围。"
                if target.state is ScopeState.WITHDRAWN
                else "该计划当前不在生效范围，撤回不改变其他范围。"
            ),
        )
    return WithdrawalResult(
        plan_id=plan_id,
        withdrawn=target,
        newly_withdrawn=(target,),
        retained=retained,
        already_withdrawn=False,
        affected_plan_ids=(target.plan_id,),
        historical_window_ids=historical,
        note=(
            f"已撤回 {target.road_id}/{target.direction} 的生效计划 {plan_id}；"
            "历史行程仍按其原始地图版本和策略状态保留，"
            f"建议新撤回版本 {withdrawal_plan_id or '(待分配)'} 自 "
            f"{(effective_from or target.effective_from).isoformat()} 起生效。"
        ),
    )


def withdraw_road_direction(
    ledger: PlanLedger,
    road_id: str,
    direction: str,
    *,
    version: str | None = None,
) -> WithdrawalResult:
    """按道路+方向（可再限定版本）局部撤回，不影响同道路对向。"""

    affected = tuple(
        item
        for item in ledger.effective
        if item.road_id == road_id
        and item.direction == direction
        and (version is None or item.version == version)
    )
    retained = tuple(
        item
        for item in ledger.effective
        if item not in affected
    )
    historical = ledger.windows_by_scope.get((road_id, direction), ())

    if not affected:
        return WithdrawalResult(
            plan_id="",
            withdrawn=None,
            newly_withdrawn=(),
            retained=retained,
            already_withdrawn=False,
            affected_plan_ids=(),
            historical_window_ids=historical,
            note=f"{road_id}/{direction} 当前没有生效计划，撤回不改变其他范围。",
        )
    return WithdrawalResult(
        plan_id="",
        withdrawn=affected[-1],
        newly_withdrawn=affected,
        retained=retained,
        already_withdrawn=False,
        affected_plan_ids=tuple(item.plan_id for item in affected),
        historical_window_ids=historical,
        note=(
            f"已撤回 {road_id}/{direction} 的 {len(affected)} 条生效计划；"
            "同道路对向范围与其他道路保持不变，历史行程不重写。"
        ),
    )
