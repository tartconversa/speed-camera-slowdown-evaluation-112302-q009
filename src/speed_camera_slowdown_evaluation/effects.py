"""策略行为评估。

设计原则：

1. **不拿摄像头前后均值直接下结论。** 弯道、路口、拥堵都会造成经过速度
   变化。主指标以“接近段末速度”为基线，衡量进入识别区后的相对减速；效应
   只在同一道路/方向/地图版本内，且交通状态、天气、采样率、识别偏移相同
   的策略窗口与未启用窗口之间比较。
2. **没有可比对照就不估计效应。** 若某范围只有策略窗口或只有对照窗口，
   返回 ``insufficient_control``，而不是把前后差异当作策略效果。主结论只
   使用同范围对照；跨道路比较单列为探索性敏感性分析，不驱动区域启用。
3. **异常值先排除再统计。** 超出物理合理性的相邻速度跳变、交通事件窗口不
   进入常态结论；排除项全部可追溯。
4. **目录条目数不是暴露数。** 曝光单位是窗口；同一范围多个目录条目仍只算
   一次经过。限速归属不唯一时不做“按限速”推断。
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from enum import StrEnum
from statistics import median
from typing import Sequence

from .exposure import ExposureReport, WindowExposure
from .models import Materials, SpeedSample, TrafficState, TrajectoryWindow

MAX_PLAUSIBLE_DECEL_MPS2 = 10.0
ZONE_LENGTH_M = 50.0
DEFAULT_MIN_PER_ARM = 2
BOOTSTRAP_ITERATIONS = 2000
BOOTSTRAP_SEED = 20261124


class MeasureStatus(StrEnum):
    MEASURED = "measured"
    MISSING_APPROACH = "missing_approach"
    MISSING_ZONE = "missing_zone"
    NOT_ELIGIBLE = "not_eligible"
    EXCLUDED_INCIDENT = "excluded_incident"


class Verdict(StrEnum):
    ESTIMATED = "estimated"
    INCONCLUSIVE = "inconclusive"
    INSUFFICIENT_CONTROL = "insufficient_control"
    NO_ELIGIBLE_WINDOWS = "no_eligible_windows"


@dataclass(frozen=True, slots=True)
class ExcludedSample:
    offset_m: float
    speed_kph: float
    reason: str

    def as_dict(self) -> dict[str, object]:
        return {"offset_m": self.offset_m, "speed_kph": self.speed_kph, "reason": self.reason}


@dataclass(frozen=True, slots=True)
class WindowProfile:
    window_id: str
    road_id: str
    direction: str
    map_version: str
    arm: str
    traffic_state: str
    weather: str
    sample_rate_hz: float
    detection_offset_m: float
    approach_speed_kph: float | None
    zone_min_speed_kph: float | None
    slowdown_kph: float | None
    recovery_kph: float | None
    strategy_enabled: bool
    limit_kph: int | None
    limit_status: str
    status: MeasureStatus
    excluded_samples: tuple[ExcludedSample, ...] = ()
    note: str = ""

    @property
    def measured(self) -> bool:
        return self.status is MeasureStatus.MEASURED

    @property
    def scope_key(self) -> tuple[str, str, str]:
        return (self.road_id, self.direction, self.map_version)

    @property
    def condition_key(self) -> tuple[str, str, float, float]:
        return (
            self.traffic_state,
            self.weather,
            self.sample_rate_hz,
            self.detection_offset_m,
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "window_id": self.window_id,
            "road_id": self.road_id,
            "direction": self.direction,
            "map_version": self.map_version,
            "arm": self.arm,
            "traffic_state": self.traffic_state,
            "weather": self.weather,
            "sample_rate_hz": self.sample_rate_hz,
            "detection_offset_m": self.detection_offset_m,
            "approach_speed_kph": self.approach_speed_kph,
            "zone_min_speed_kph": self.zone_min_speed_kph,
            "slowdown_kph": self.slowdown_kph,
            "recovery_kph": self.recovery_kph,
            "strategy_enabled": self.strategy_enabled,
            "limit_kph": self.limit_kph,
            "limit_status": self.limit_status,
            "status": str(self.status),
            "excluded_samples": [item.as_dict() for item in self.excluded_samples],
            "note": self.note,
        }


@dataclass(frozen=True, slots=True)
class MatchedStratum:
    """一组成对条件完全相同的策略窗口与对照窗口。"""

    road_ids: tuple[str, ...]
    direction: str | None
    map_version: str | None
    limit_kph: int | None
    traffic_state: str
    weather: str
    sample_rate_hz: float
    detection_offset_m: float
    strategy_window_ids: tuple[str, ...]
    control_window_ids: tuple[str, ...]
    strategy_slowdowns_kph: tuple[float, ...]
    control_slowdowns_kph: tuple[float, ...]
    strategy_slowdown_median_kph: float
    control_slowdown_median_kph: float
    difference_kph: float

    @property
    def window_count(self) -> int:
        return len(self.strategy_window_ids) + len(self.control_window_ids)

    def as_dict(self) -> dict[str, object]:
        return {
            "road_ids": list(self.road_ids),
            "direction": self.direction,
            "map_version": self.map_version,
            "limit_kph": self.limit_kph,
            "traffic_state": self.traffic_state,
            "weather": self.weather,
            "sample_rate_hz": self.sample_rate_hz,
            "detection_offset_m": self.detection_offset_m,
            "strategy_window_ids": list(self.strategy_window_ids),
            "control_window_ids": list(self.control_window_ids),
            "strategy_slowdowns_kph": list(self.strategy_slowdowns_kph),
            "control_slowdowns_kph": list(self.control_slowdowns_kph),
            "strategy_slowdown_median_kph": self.strategy_slowdown_median_kph,
            "control_slowdown_median_kph": self.control_slowdown_median_kph,
            "difference_kph": self.difference_kph,
        }


@dataclass(frozen=True, slots=True)
class EffectEstimate:
    label: str
    analysis: str
    road_id: str | None
    direction: str | None
    map_version: str | None
    limit_kph: int | None
    verdict: Verdict
    strategy_windows: int
    control_windows: int
    comparable_strata: int
    effect_kph: float | None = None
    ci_low_kph: float | None = None
    ci_high_kph: float | None = None
    strategy_slowdown_median_kph: float | None = None
    control_slowdown_median_kph: float | None = None
    note: str = ""

    def as_dict(self) -> dict[str, object]:
        return {
            "label": self.label,
            "analysis": self.analysis,
            "road_id": self.road_id,
            "direction": self.direction,
            "map_version": self.map_version,
            "limit_kph": self.limit_kph,
            "verdict": str(self.verdict),
            "strategy_windows": self.strategy_windows,
            "control_windows": self.control_windows,
            "comparable_strata": self.comparable_strata,
            "effect_kph": self.effect_kph,
            "ci_low_kph": self.ci_low_kph,
            "ci_high_kph": self.ci_high_kph,
            "strategy_slowdown_median_kph": self.strategy_slowdown_median_kph,
            "control_slowdown_median_kph": self.control_slowdown_median_kph,
            "note": self.note,
        }


@dataclass(frozen=True, slots=True)
class EffectReport:
    profiles: tuple[WindowProfile, ...]
    strata: tuple[MatchedStratum, ...]
    scopes: tuple[EffectEstimate, ...]
    limits: tuple[EffectEstimate, ...]
    overall: EffectEstimate
    cross_scope: tuple[EffectEstimate, ...]
    excluded_counts: dict[str, int]
    notes: tuple[str, ...]
    min_per_arm: int = DEFAULT_MIN_PER_ARM

    def as_dict(self) -> dict[str, object]:
        return {
            "min_per_arm": self.min_per_arm,
            "notes": list(self.notes),
            "excluded_counts": dict(self.excluded_counts),
            "overall": self.overall.as_dict(),
            "scopes": [item.as_dict() for item in self.scopes],
            "limits": [item.as_dict() for item in self.limits],
            "cross_scope": [item.as_dict() for item in self.cross_scope],
            "strata": [item.as_dict() for item in self.strata],
            "windows": [item.as_dict() for item in self.profiles],
        }


def evaluate_strategy(
    materials: Materials,
    exposure: ExposureReport,
    *,
    min_per_arm: int = DEFAULT_MIN_PER_ARM,
    bootstrapping: bool = True,
) -> EffectReport:
    windows = {window.window_id: window for window in materials.windows}
    samples_by_window: dict[str, list[SpeedSample]] = defaultdict(list)
    for sample in materials.samples:
        samples_by_window[sample.window_id].append(sample)

    profiles = tuple(
        _profile_window(
            item,
            windows[item.window_id],
            samples_by_window.get(item.window_id, []),
        )
        for item in exposure.exposures
    )
    primary = [
        profile
        for profile in profiles
        if profile.measured and profile.traffic_state != TrafficState.INCIDENT.value
    ]

    scope_groups: dict[tuple[str, str, str], list[WindowProfile]] = defaultdict(list)
    for profile in primary:
        scope_groups[profile.scope_key].append(profile)

    scopes: list[EffectEstimate] = []
    all_strata: list[MatchedStratum] = []
    for scope_key, group in sorted(scope_groups.items()):
        strata = _build_strata(group, min_per_arm, within_scope=True)
        all_strata.extend(strata)
        scopes.append(
            _estimate(
                label=f"{scope_key[0]}/{scope_key[1]}",
                analysis="within_scope",
                road_id=scope_key[0],
                direction=scope_key[1],
                map_version=scope_key[2],
                limit_kph=_common_limit(group),
                group=group,
                strata=strata,
                bootstrapping=bootstrapping,
                note=(
                    "同范围出现多个限速值，无法把经过唯一归到某个限速。"
                    if _limit_is_ambiguous(group)
                    else ""
                ),
            )
        )

    overall_strata = _build_strata(primary, min_per_arm, within_scope=True)
    overall = _estimate(
        label="全部范围（同范围对照）",
        analysis="within_scope",
        road_id=None,
        direction=None,
        map_version=None,
        limit_kph=None,
        group=primary,
        strata=overall_strata,
        bootstrapping=bootstrapping,
        note="只在各自同范围对照内汇总，不同道路不互相充当对照。",
    )

    cross_scope = _cross_scope_sensitivity(primary, min_per_arm, bootstrapping)
    limits = tuple(
        _limit_sensitivity(primary, min_per_arm, bootstrapping)
    )

    excluded = Counter()
    for profile in profiles:
        excluded[str(profile.status)] += 1
        excluded["implausible_samples"] += len(profile.excluded_samples)
    for profile in profiles:
        if profile.traffic_state == TrafficState.INCIDENT.value:
            excluded["incident_traffic_windows"] += 1

    notes = (
        "主指标为“接近段末速度 − 识别区最低速度”，正值表示进入摄像头区域后减速。",
        "主结论（within_scope）只比较同一道路/方向/地图版本，且交通状态、天气、"
        "采样率、识别偏移相同的策略窗口与未启用窗口。",
        "cross_scope 与 limits 是跨道路的探索性敏感性分析，仅用于提出假设，"
        "不能用于区域启用决定，也不能替代同范围对照。",
        "交通事件窗口和物理上不合理的速度跳变不进入常态结论，但在明细中保留。",
        "限速仅在窗口能唯一对应一个确认目录条目时用于分组；否则标记为歧义。",
    )
    return EffectReport(
        profiles=profiles,
        strata=tuple(all_strata),
        scopes=tuple(scopes),
        limits=limits,
        overall=overall,
        cross_scope=tuple(cross_scope),
        excluded_counts=dict(excluded),
        notes=notes,
        min_per_arm=min_per_arm,
    )


def _profile_window(
    exposure: WindowExposure,
    window: TrajectoryWindow,
    samples: Sequence[SpeedSample],
) -> WindowProfile:
    ordered = sorted(samples, key=lambda sample: sample.offset_m)
    limit_status, limit = _limit_for_exposure(exposure)

    base = dict(
        window_id=window.window_id,
        road_id=window.road_id,
        direction=window.direction,
        map_version=exposure.map_version or "",
        arm=exposure.arm or "",
        traffic_state=str(window.traffic_state),
        weather=window.weather,
        sample_rate_hz=window.sample_rate_hz,
        detection_offset_m=exposure.detection_offset_m or 0.0,
        strategy_enabled=bool(exposure.strategy_enabled),
        limit_kph=limit,
        limit_status=limit_status,
    )
    empty = dict(
        approach_speed_kph=None,
        zone_min_speed_kph=None,
        slowdown_kph=None,
        recovery_kph=None,
    )

    if not exposure.eligible or exposure.arm is None:
        return WindowProfile(
            status=MeasureStatus.NOT_ELIGIBLE,
            note=f"窗口曝光状态为 {exposure.status}，不进入效应比较。",
            **empty,
            **base,
        )

    if window.traffic_state is TrafficState.INCIDENT:
        return WindowProfile(
            status=MeasureStatus.EXCLUDED_INCIDENT,
            note="交通事件窗口不进入常态效应估计。",
            **empty,
            **base,
        )

    cleaned, excluded = _drop_implausible(ordered, window.sample_rate_hz)
    detection = exposure.detection_offset_m
    assert detection is not None

    before = [sample for sample in cleaned if sample.offset_m < detection]
    zone = [
        sample
        for sample in cleaned
        if detection <= sample.offset_m <= detection + ZONE_LENGTH_M
    ]
    after = [sample for sample in cleaned if sample.offset_m > detection + ZONE_LENGTH_M]

    if not before:
        return WindowProfile(
            status=MeasureStatus.MISSING_APPROACH,
            approach_speed_kph=None,
            zone_min_speed_kph=min((s.speed_kph for s in zone), default=None),
            slowdown_kph=None,
            recovery_kph=None,
            excluded_samples=excluded,
            note="识别点前没有可用样本，无法建立接近段基线。",
            **base,
        )
    if not zone:
        return WindowProfile(
            status=MeasureStatus.MISSING_ZONE,
            approach_speed_kph=before[-1].speed_kph,
            zone_min_speed_kph=None,
            slowdown_kph=None,
            recovery_kph=None,
            excluded_samples=excluded,
            note="识别区 50 m 内没有可用样本。",
            **base,
        )

    approach = before[-1].speed_kph
    zone_min = min(sample.speed_kph for sample in zone)
    recovery = (after[-1].speed_kph - zone_min) if after else None
    note = f"已排除 {len(excluded)} 个物理上不合理的样本。" if excluded else ""
    return WindowProfile(
        status=MeasureStatus.MEASURED,
        approach_speed_kph=approach,
        zone_min_speed_kph=zone_min,
        slowdown_kph=approach - zone_min,
        recovery_kph=recovery,
        excluded_samples=excluded,
        note=note,
        **base,
    )


def _limit_for_exposure(exposure: WindowExposure) -> tuple[str, int | None]:
    if not exposure.limit_kph_values:
        return "none", None
    if len(exposure.limit_kph_values) == 1:
        return "unique", exposure.limit_kph_values[0]
    return "ambiguous", None


def _common_limit(group: Sequence[WindowProfile]) -> int | None:
    limits = {profile.limit_kph for profile in group}
    if len(limits) == 1 and None not in limits:
        return next(iter(limits))
    return None


def _limit_is_ambiguous(group: Sequence[WindowProfile]) -> bool:
    return any(profile.limit_status == "ambiguous" for profile in group)


def _drop_implausible(
    samples: Sequence[SpeedSample], sample_rate_hz: float
) -> tuple[list[SpeedSample], tuple[ExcludedSample, ...]]:
    if not samples:
        return [], ()
    kept = [samples[0]]
    excluded: list[ExcludedSample] = []
    for previous, current in zip(samples, samples[1:]):
        distance = current.offset_m - previous.offset_m
        avg_speed_mps = ((previous.speed_kph + current.speed_kph) / 2.0) / 3.6
        if avg_speed_mps <= 0 or distance <= 0:
            kept.append(current)
            continue
        dt = max(distance / avg_speed_mps, 1.0 / sample_rate_hz if sample_rate_hz > 0 else 0.0)
        delta_v = abs(current.speed_kph - previous.speed_kph) / 3.6
        acceleration = delta_v / dt if dt > 0 else float("inf")
        if acceleration > MAX_PLAUSIBLE_DECEL_MPS2:
            excluded.append(
                ExcludedSample(
                    offset_m=current.offset_m,
                    speed_kph=current.speed_kph,
                    reason=(
                        f"相对前一有效样本速度变化 "
                        f"{current.speed_kph - previous.speed_kph:+.1f} kph，"
                        f"折合 {acceleration:.1f} m/s²，超过合理阈值 "
                        f"{MAX_PLAUSIBLE_DECEL_MPS2:.0f} m/s²。"
                    ),
                )
            )
            continue
        kept.append(current)
    return kept, tuple(excluded)


def _build_strata(
    profiles: Sequence[WindowProfile],
    min_per_arm: int,
    *,
    within_scope: bool,
    limit: int | None = None,
) -> list[MatchedStratum]:
    """按条件分层，只有两臂窗口数都达标的分层才参与估计。"""

    grouped: dict[tuple, list[WindowProfile]] = defaultdict(list)
    for profile in profiles:
        if within_scope:
            key = (profile.scope_key, profile.condition_key)
        else:
            key = (profile.condition_key, profile.limit_kph)
        grouped[key].append(profile)

    strata: list[MatchedStratum] = []
    for key, group in sorted(grouped.items(), key=lambda item: str(item[0])):
        strategy = sorted(
            (profile for profile in group if profile.arm == "strategy"),
            key=lambda profile: profile.window_id,
        )
        control = sorted(
            (profile for profile in group if profile.arm == "control"),
            key=lambda profile: profile.window_id,
        )
        if len(strategy) < min_per_arm or len(control) < min_per_arm:
            continue
        if not within_scope:
            # 跨道路敏感性：两臂都必须覆盖至少两条道路，避免单一道路偶然主导。
            if len({profile.road_id for profile in strategy}) < 2:
                continue
            if len({profile.road_id for profile in control}) < 2:
                continue
        strategy_slowdowns = tuple(
            profile.slowdown_kph for profile in strategy if profile.slowdown_kph is not None
        )
        control_slowdowns = tuple(
            profile.slowdown_kph for profile in control if profile.slowdown_kph is not None
        )
        if not strategy_slowdowns or not control_slowdowns:
            continue
        strategy_median = median(strategy_slowdowns)
        control_median = median(control_slowdowns)
        scope_key = strategy[0].scope_key
        strata.append(
            MatchedStratum(
                road_ids=tuple(sorted({profile.road_id for profile in group})),
                direction=scope_key[1] if within_scope else None,
                map_version=scope_key[2] if within_scope else None,
                limit_kph=limit if limit is not None else _common_limit(group),
                traffic_state=strategy[0].traffic_state,
                weather=strategy[0].weather,
                sample_rate_hz=strategy[0].sample_rate_hz,
                detection_offset_m=strategy[0].detection_offset_m,
                strategy_window_ids=tuple(profile.window_id for profile in strategy),
                control_window_ids=tuple(profile.window_id for profile in control),
                strategy_slowdowns_kph=strategy_slowdowns,
                control_slowdowns_kph=control_slowdowns,
                strategy_slowdown_median_kph=round(strategy_median, 2),
                control_slowdown_median_kph=round(control_median, 2),
                difference_kph=round(strategy_median - control_median, 2),
            )
        )
    return strata


def _estimate(
    *,
    label: str,
    analysis: str,
    road_id: str | None,
    direction: str | None,
    map_version: str | None,
    limit_kph: int | None,
    group: Sequence[WindowProfile],
    strata: Sequence[MatchedStratum],
    bootstrapping: bool,
    note: str,
) -> EffectEstimate:
    strategy_count = sum(1 for profile in group if profile.arm == "strategy")
    control_count = sum(1 for profile in group if profile.arm == "control")
    base = dict(
        label=label,
        analysis=analysis,
        road_id=road_id,
        direction=direction,
        map_version=map_version,
        limit_kph=limit_kph,
        strategy_windows=strategy_count,
        control_windows=control_count,
        comparable_strata=len(strata),
    )

    if strategy_count == 0 and control_count == 0:
        return EffectEstimate(verdict=Verdict.NO_ELIGIBLE_WINDOWS, note=note, **base)
    if strategy_count == 0 or control_count == 0:
        return EffectEstimate(
            verdict=Verdict.INSUFFICIENT_CONTROL,
            note=(note + " " if note else "")
            + "该范围只有单一启用状态，缺少可比的未启用对照，不能估计策略效应。",
            **base,
        )
    if not strata:
        return EffectEstimate(
            verdict=Verdict.INSUFFICIENT_CONTROL,
            note=(note + " " if note else "")
            + "没有同时具备策略与未启用窗口、且条件一致的分层；不能估计策略效应。",
            **base,
        )

    differences = [stratum.difference_kph for stratum in strata]
    effect = median(differences)
    low, high = _bootstrap_ci(strata) if bootstrapping else (None, None)
    degenerate = low is not None and high is not None and (high - low) <= 1e-9
    if low is not None and high is not None and low > 0 and not degenerate:
        verdict = Verdict.ESTIMATED
    else:
        verdict = Verdict.INCONCLUSIVE
    extra = ""
    if degenerate:
        extra = (
            " 重采样区间退化（分层内各窗口结果相同），无法估计不确定性，"
            "需要更多不同条件的窗口。"
        )
    return EffectEstimate(
        verdict=verdict,
        effect_kph=round(effect, 2),
        ci_low_kph=None if low is None else round(low, 2),
        ci_high_kph=None if high is None else round(high, 2),
        strategy_slowdown_median_kph=_pooled_median(strata, "strategy"),
        control_slowdown_median_kph=_pooled_median(strata, "control"),
        note=note + extra,
        **base,
    )


def _pooled_median(strata: Sequence[MatchedStratum], arm: str) -> float:
    values = [
        stratum.strategy_slowdown_median_kph
        if arm == "strategy"
        else stratum.control_slowdown_median_kph
        for stratum in strata
    ]
    return round(median(values), 2)


def _bootstrap_ci(
    strata: Sequence[MatchedStratum],
    iterations: int = BOOTSTRAP_ITERATIONS,
    seed: int = BOOTSTRAP_SEED,
) -> tuple[float | None, float | None]:
    """在分层内分别重采样策略与对照窗口，再取差值中位数。"""

    import random

    if not strata:
        return (None, None)
    rng = random.Random(seed)
    effects: list[float] = []
    for _ in range(iterations):
        differences = []
        for stratum in strata:
            boot_strategy = [
                stratum.strategy_slowdowns_kph[rng.randrange(len(stratum.strategy_slowdowns_kph))]
                for _ in stratum.strategy_slowdowns_kph
            ]
            boot_control = [
                stratum.control_slowdowns_kph[rng.randrange(len(stratum.control_slowdowns_kph))]
                for _ in stratum.control_slowdowns_kph
            ]
            differences.append(median(boot_strategy) - median(boot_control))
        effects.append(median(differences))
    effects.sort()
    low_index = int(0.025 * (iterations - 1))
    high_index = int(0.975 * (iterations - 1))
    return (effects[low_index], effects[high_index])


def _limit_sensitivity(
    profiles: Sequence[WindowProfile], min_per_arm: int, bootstrapping: bool
) -> list[EffectEstimate]:
    """按限速分组的跨道路敏感性分析（探索性）。"""

    by_limit: dict[int, list[WindowProfile]] = defaultdict(list)
    for profile in profiles:
        if profile.limit_status == "unique" and profile.limit_kph is not None:
            by_limit[profile.limit_kph].append(profile)

    estimates: list[EffectEstimate] = []
    for limit, group in sorted(by_limit.items()):
        strata = _build_strata(group, min_per_arm, within_scope=False, limit=limit)
        estimates.append(
            _estimate(
                label=f"限速 {limit} kph",
                analysis="cross_scope_limit",
                road_id=None,
                direction=None,
                map_version=None,
                limit_kph=limit,
                group=group,
                strata=strata,
                bootstrapping=bootstrapping,
                note=(
                    "跨道路按限速汇总，仅作探索性参考；道路几何与路口差异"
                    "未被同范围对照吸收。"
                ),
            )
        )
    return estimates


def _cross_scope_sensitivity(
    profiles: Sequence[WindowProfile], min_per_arm: int, bootstrapping: bool
) -> list[EffectEstimate]:
    """不按限速、仅按条件分层的跨道路敏感性分析（探索性）。"""

    strata = _build_strata(profiles, min_per_arm, within_scope=False)
    if not strata:
        return []
    return [
        _estimate(
            label="跨道路条件匹配（探索性）",
            analysis="cross_scope",
            road_id=None,
            direction=None,
            map_version=None,
            limit_kph=None,
            group=profiles,
            strata=strata,
            bootstrapping=bootstrapping,
            note=(
                "不同道路的弯道、路口与道路层级差异无法被完全吸收；"
                "该结果不得直接用于启用决定。"
            ),
        )
    ]
