"""领域模型：原始记录与派生视图。

所有字段保留来源 CSV 中的字符串值，避免在加载阶段丢失信息；需要
数值或时间时由使用方显式转换，并记录转换失败而不是静默为零。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from enum import StrEnum


class ReviewState(StrEnum):
    CONFIRMED = "confirmed"
    NEEDS_REVIEW = "needs-review"
    WITHDRAWN = "withdrawn"


class PlanStatus(StrEnum):
    ACCEPTED = "accepted"
    PARTIAL = "partial"
    PROPOSED = "proposed"
    WITHDRAWN = "withdrawn"


class TrafficState(StrEnum):
    FREE = "free"
    CONGESTED = "congested"
    INCIDENT = "incident"


@dataclass(frozen=True, slots=True)
class Camera:
    camera_id: str
    road_id: str
    direction: str
    km_marker: float
    limit_kph: int
    map_version: str
    valid_from: date | None
    valid_to: date | None
    review_state: ReviewState
    source_row: int = field(default=0, compare=False)

    @property
    def usable_for_matching(self) -> bool:
        """只有已确认的目录条目可用于把轨迹与限速位置关联。

        待复核条目位置不可信，撤回条目表示目录不再承认该摄像头；二者都可
        作为资料质量问题报告，但不参与暴露计算。
        """
        return self.review_state is ReviewState.CONFIRMED

    def covers(self, day: date) -> bool:
        if self.valid_from is not None and day < self.valid_from:
            return False
        if self.valid_to is not None and day >= self.valid_to:
            return False
        return True


@dataclass(frozen=True, slots=True)
class TrajectoryWindow:
    window_id: str
    vehicle_id: str
    road_id: str
    direction: str
    started_at: datetime
    sample_rate_hz: float
    traffic_state: TrafficState
    weather: str
    source_row: int = field(default=0, compare=False)


@dataclass(frozen=True, slots=True)
class SpeedSample:
    window_id: str
    offset_m: float
    speed_kph: float
    strategy_enabled: bool
    camera_seen: bool
    map_version: str
    source_row: int = field(default=0, compare=False)


@dataclass(frozen=True, slots=True)
class RegionPlan:
    plan_id: str
    region: str
    road_id: str
    direction: str
    version: str
    status: PlanStatus
    effective_from: date
    receipt_id: str
    source_row: int = field(default=0, compare=False)

    @property
    def key(self) -> tuple[str, str, str]:
        """计划作用范围由道路与方向共同确定；仅道路匹配会误撤回对向计划。"""
        return (self.road_id, self.direction, self.version)

    @property
    def is_effective(self) -> bool:
        return self.status in (PlanStatus.ACCEPTED, PlanStatus.PARTIAL)


@dataclass(frozen=True, slots=True)
class Materials:
    cameras: tuple[Camera, ...]
    windows: tuple[TrajectoryWindow, ...]
    samples: tuple[SpeedSample, ...]
    plans: tuple[RegionPlan, ...]
