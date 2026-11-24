"""把轨迹窗口与目录条目关联，形成“有效暴露”。

关键约束：

* 只有 ``confirmed`` 且处于有效期、且与样本地图版本一致的目录条目才可
  用于关联；``needs-review`` 与 ``withdrawn`` 都是有效业务事实，但不会
  被解释成摄像头存在或零事件。
* 曝光单位是“窗口”（一次可比较的经过），不是目录条目数。同一道路/方向
  下可能有多个目录条目，但每次经过只能算一次暴露。
* 方向不一致的条目不会被拿来凑暴露；若某窗口只有对向条目，会记录为
  ``direction_mismatch`` 而不是静默跳过。
* ``camera_seen`` 是车辆侧的实际识别证据。地图里有条目但窗口内从未识别，
  记为 ``camera_not_observed``，不作为暴露。
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date
from enum import StrEnum

from .models import Camera, Materials, ReviewState, SpeedSample, TrajectoryWindow


class ExposureStatus(StrEnum):
    ELIGIBLE = "eligible"
    NO_CONFIRMED_CAMERA = "no_confirmed_camera"
    DIRECTION_MISMATCH = "direction_mismatch"
    MAP_VERSION_MISMATCH = "map_version_mismatch"
    CAMERA_NOT_OBSERVED = "camera_not_observed"
    INCOMPLETE_GEOMETRY = "incomplete_geometry"
    MIXED_STRATEGY = "mixed_strategy_state"
    MULTIPLE_MAP_VERSIONS = "multiple_map_versions"


@dataclass(frozen=True, slots=True)
class WindowExposure:
    window_id: str
    vehicle_id: str
    road_id: str
    direction: str
    started_at: str
    day: date
    traffic_state: str
    weather: str
    sample_rate_hz: float
    strategy_enabled: bool | None
    map_version: str | None
    status: ExposureStatus
    camera_ids: tuple[str, ...] = ()
    camera_entry_count: int = 0
    limit_kph_values: tuple[int, ...] = ()
    detection_offset_m: float | None = None
    sample_count: int = 0
    note: str = ""

    @property
    def eligible(self) -> bool:
        return self.status is ExposureStatus.ELIGIBLE

    @property
    def arm(self) -> str | None:
        if not self.eligible or self.strategy_enabled is None:
            return None
        return "strategy" if self.strategy_enabled else "control"

    @property
    def scope_key(self) -> tuple[str, str, str]:
        return (self.road_id, self.direction, self.map_version or "")

    def as_dict(self) -> dict[str, object]:
        return {
            "window_id": self.window_id,
            "vehicle_id": self.vehicle_id,
            "road_id": self.road_id,
            "direction": self.direction,
            "started_at": self.started_at,
            "traffic_state": self.traffic_state,
            "weather": self.weather,
            "sample_rate_hz": self.sample_rate_hz,
            "strategy_enabled": self.strategy_enabled,
            "map_version": self.map_version,
            "status": str(self.status),
            "eligible": self.eligible,
            "arm": self.arm,
            "camera_ids": list(self.camera_ids),
            "camera_entry_count": self.camera_entry_count,
            "limit_kph_values": list(self.limit_kph_values),
            "detection_offset_m": self.detection_offset_m,
            "sample_count": self.sample_count,
            "note": self.note,
        }


@dataclass(frozen=True, slots=True)
class ScopeCoverage:
    road_id: str
    direction: str
    camera_entries: int
    confirmed_camera_entries: int
    windows: int
    eligible_windows: int
    strategy_windows: int
    control_windows: int
    note: str = ""

    def as_dict(self) -> dict[str, object]:
        return {
            "road_id": self.road_id,
            "direction": self.direction,
            "camera_entries": self.camera_entries,
            "confirmed_camera_entries": self.confirmed_camera_entries,
            "windows": self.windows,
            "eligible_windows": self.eligible_windows,
            "strategy_windows": self.strategy_windows,
            "control_windows": self.control_windows,
            "note": self.note,
            "counting_note": "曝光按窗口计数，不按目录条目计数。",
        }


@dataclass(frozen=True, slots=True)
class ExposureReport:
    exposures: tuple[WindowExposure, ...]
    scope_coverage: tuple[ScopeCoverage, ...]
    suspect_cameras: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()

    @property
    def eligible(self) -> tuple[WindowExposure, ...]:
        return tuple(item for item in self.exposures if item.eligible)

    @property
    def counts(self) -> dict[str, int]:
        return dict(Counter(str(item.status) for item in self.exposures))

    def for_scope(self, road_id: str, direction: str) -> tuple[WindowExposure, ...]:
        return tuple(
            item
            for item in self.exposures
            if item.road_id == road_id and item.direction == direction
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "window_count": len(self.exposures),
            "eligible_window_count": len(self.eligible),
            "status_counts": self.counts,
            "suspect_camera_ids": list(self.suspect_cameras),
            "notes": list(self.notes),
            "scopes": [scope.as_dict() for scope in self.scope_coverage],
            "windows": [item.as_dict() for item in self.exposures],
        }


def build_exposures(materials: Materials) -> ExposureReport:
    samples_by_window: dict[str, list[SpeedSample]] = defaultdict(list)
    for sample in materials.samples:
        samples_by_window[sample.window_id].append(sample)

    windows_by_scope: dict[tuple[str, str], list[TrajectoryWindow]] = defaultdict(list)
    for window in materials.windows:
        windows_by_scope[(window.road_id, window.direction)].append(window)

    suspect_cameras: set[str] = set()
    confirmed_by_scope_version: dict[tuple[str, str, str], list[Camera]] = defaultdict(list)
    confirmed_by_road_version: dict[tuple[str, str], list[Camera]] = defaultdict(list)
    all_by_scope_version: dict[tuple[str, str, str], list[Camera]] = defaultdict(list)
    confirmed_by_road_dir: dict[tuple[str, str], list[Camera]] = defaultdict(list)
    for camera in materials.cameras:
        all_by_scope_version[(camera.road_id, camera.direction, camera.map_version)].append(camera)
        if camera.review_state is ReviewState.CONFIRMED:
            confirmed_by_scope_version[(camera.road_id, camera.direction, camera.map_version)].append(camera)
            confirmed_by_road_version[(camera.road_id, camera.map_version)].append(camera)
            confirmed_by_road_dir[(camera.road_id, camera.direction)].append(camera)
            opposite = _opposite(camera.direction)
            if (
                not windows_by_scope.get((camera.road_id, camera.direction))
                and windows_by_scope.get((camera.road_id, opposite))
            ):
                # 该条目记录的方向没有任何经过车辆，而道路对向有轨迹。这可能是
                # 目录方向错误，也可能是单向交通；在没有更正前不能把它当成
                # 同方向暴露。
                suspect_cameras.add(camera.camera_id)

    exposures: list[WindowExposure] = []
    for window in materials.windows:
        samples = sorted(samples_by_window.get(window.window_id, []), key=lambda s: s.offset_m)
        exposures.append(
            _match_window(
                window,
                samples,
                confirmed_by_scope_version,
                confirmed_by_road_version,
                all_by_scope_version,
                confirmed_by_road_dir,
                suspect_cameras,
            )
        )

    coverage = _build_coverage(materials, exposures)
    notes = (
        "同一道路/方向/地图版本下存在多个目录条目时，单次经过仍只计一次暴露；"
        "缺少窗口起始公里标，无法把经过唯一归到某个条目。",
        "窗口曝光只覆盖当前资料中的道路与方向组合；目录条目数不等于有效暴露数。",
    )
    return ExposureReport(
        exposures=tuple(exposures),
        scope_coverage=coverage,
        suspect_cameras=tuple(sorted(suspect_cameras)),
        notes=notes,
    )


def _match_window(
    window: TrajectoryWindow,
    samples: list[SpeedSample],
    confirmed_by_scope_version: dict[tuple[str, str, str], list[Camera]],
    confirmed_by_road_version: dict[tuple[str, str], list[Camera]],
    all_by_scope_version: dict[tuple[str, str, str], list[Camera]],
    confirmed_by_road_dir: dict[tuple[str, str], list[Camera]],
    suspect_cameras: set[str],
) -> WindowExposure:
    day = window.started_at.date()
    map_versions = {sample.map_version for sample in samples}
    strategies = {sample.strategy_enabled for sample in samples}
    strategy = strategies.pop() if len(strategies) == 1 else None
    map_version = map_versions.pop() if len(map_versions) == 1 else None

    common = dict(
        window_id=window.window_id,
        vehicle_id=window.vehicle_id,
        road_id=window.road_id,
        direction=window.direction,
        started_at=window.started_at.isoformat(),
        day=day,
        traffic_state=str(window.traffic_state),
        weather=window.weather,
        sample_rate_hz=window.sample_rate_hz,
        strategy_enabled=strategy,
        map_version=map_version,
        sample_count=len(samples),
    )

    if not samples:
        return WindowExposure(status=ExposureStatus.INCOMPLETE_GEOMETRY, note="窗口没有速度样本。", **common)
    if len(map_versions) > 1:
        return WindowExposure(
            status=ExposureStatus.MULTIPLE_MAP_VERSIONS,
            note=f"窗口包含多个地图版本 {sorted(map_versions)}，无法关联。",
            **common,
        )
    if len(strategies) > 1:
        return WindowExposure(
            status=ExposureStatus.MIXED_STRATEGY,
            note="窗口内策略启用状态不一致，不能作为单一处理或对照。",
            **common,
        )

    assert map_version is not None  # 已在上面排除多版本
    candidates = [
        camera
        for camera in confirmed_by_scope_version.get((window.road_id, window.direction, map_version), [])
        if camera.covers(day) and camera.camera_id not in suspect_cameras
    ]

    if not candidates:
        opposite = _opposite(window.direction)
        opposite_entries = confirmed_by_scope_version.get(
            (window.road_id, opposite, map_version), []
        )
        if any(camera.covers(day) for camera in opposite_entries):
            return WindowExposure(
                status=ExposureStatus.DIRECTION_MISMATCH,
                note=(
                    "同道路、同地图版本只有对向条目的确认记录；方向不一致的条目"
                    "不能用于本窗口的暴露或效果比较。"
                ),
                **common,
            )
        if confirmed_by_road_version.get((window.road_id, map_version)):
            return WindowExposure(
                status=ExposureStatus.MAP_VERSION_MISMATCH,
                note="同道路存在确认条目，但没有与样本地图版本一致的记录。",
                **common,
            )
        if all_by_scope_version.get((window.road_id, window.direction, map_version)):
            return WindowExposure(
                status=ExposureStatus.NO_CONFIRMED_CAMERA,
                note="同范围只有待复核或已撤回条目，目录记录不构成有效暴露。",
                **common,
            )
        if confirmed_by_road_dir.get((window.road_id, window.direction)):
            return WindowExposure(
                status=ExposureStatus.MAP_VERSION_MISMATCH,
                note="同道路/方向有确认条目，但地图版本与轨迹样本不一致。",
                **common,
            )
        if confirmed_by_road_dir.get((window.road_id, opposite)):
            return WindowExposure(
                status=ExposureStatus.DIRECTION_MISMATCH,
                note="同道路只有对向确认条目；不能用于本窗口。",
                **common,
            )
        return WindowExposure(
            status=ExposureStatus.NO_CONFIRMED_CAMERA,
            note="目录中没有可用于该道路/方向的确认摄像头。",
            **common,
        )

    observed_offsets = [sample.offset_m for sample in samples if sample.camera_seen]
    if not observed_offsets:
        return WindowExposure(
            status=ExposureStatus.CAMERA_NOT_OBSERVED,
            camera_ids=tuple(sorted(camera.camera_id for camera in candidates)),
            camera_entry_count=len(candidates),
            limit_kph_values=tuple(sorted({camera.limit_kph for camera in candidates})),
            note="地图条目存在，但窗口内没有车辆识别记录；不能作为实际暴露。",
            **common,
        )

    detection = min(observed_offsets)
    before = any(sample.offset_m < detection for sample in samples)
    after = any(sample.offset_m > detection for sample in samples)
    camera_ids = tuple(sorted(camera.camera_id for camera in candidates))
    limits = tuple(sorted({camera.limit_kph for camera in candidates}))
    if not (before and after):
        return WindowExposure(
            status=ExposureStatus.INCOMPLETE_GEOMETRY,
            camera_ids=camera_ids,
            camera_entry_count=len(candidates),
            limit_kph_values=limits,
            detection_offset_m=detection,
            note="识别点位于窗口边缘，缺少摄像头前或后的样本，无法形成前后比较。",
            **common,
        )

    return WindowExposure(
        status=ExposureStatus.ELIGIBLE,
        camera_ids=camera_ids,
        camera_entry_count=len(candidates),
        limit_kph_values=limits,
        detection_offset_m=detection,
        note=(
            "可比较；同一范围有多个目录条目，但暴露按窗口计一次。"
            if len(candidates) > 1
            else "可比较。"
        ),
        **common,
    )


def _build_coverage(
    materials: Materials, exposures: list[WindowExposure]
) -> tuple[ScopeCoverage, ...]:
    cameras_by_scope: dict[tuple[str, str], list[Camera]] = defaultdict(list)
    for camera in materials.cameras:
        cameras_by_scope[(camera.road_id, camera.direction)].append(camera)

    exposures_by_scope: dict[tuple[str, str], list[WindowExposure]] = defaultdict(list)
    for exposure in exposures:
        exposures_by_scope[(exposure.road_id, exposure.direction)].append(exposure)

    scopes = sorted(set(cameras_by_scope) | set(exposures_by_scope))
    result: list[ScopeCoverage] = []
    for road_id, direction in scopes:
        entries = cameras_by_scope.get((road_id, direction), [])
        items = exposures_by_scope.get((road_id, direction), [])
        eligible = [item for item in items if item.eligible]
        note = ""
        if entries and not items:
            note = "目录有条目但当前资料没有轨迹窗口。"
        elif items and not entries:
            note = "有轨迹窗口但目录没有该方向条目。"
        elif entries and not any(camera.review_state is ReviewState.CONFIRMED for camera in entries):
            note = "目录条目均未确认或已撤回。"
        result.append(
            ScopeCoverage(
                road_id=road_id,
                direction=direction,
                camera_entries=len(entries),
                confirmed_camera_entries=sum(
                    1 for camera in entries if camera.review_state is ReviewState.CONFIRMED
                ),
                windows=len(items),
                eligible_windows=len(eligible),
                strategy_windows=sum(1 for item in eligible if item.arm == "strategy"),
                control_windows=sum(1 for item in eligible if item.arm == "control"),
                note=note,
            )
        )
    return tuple(result)


def _opposite(direction: str) -> str:
    return {"N": "S", "S": "N", "E": "W", "W": "E"}.get(direction, "")
