"""从契约 CSV 构建练习用夹具数据。

真实资料按契约放在 ``data/``；测试与演示需要可控的小样本时，直接构造
``Materials``，避免用预测结果反推输入。字段含义与 ``docs/资料契约.md`` 一致。
"""

from __future__ import annotations

from datetime import date, datetime, timezone

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


def camera(
    camera_id: str,
    road_id: str,
    direction: str,
    *,
    km_marker: float = 100.0,
    limit_kph: int = 50,
    map_version: str = "M-1",
    review_state: ReviewState = ReviewState.CONFIRMED,
    valid_from: date = date(2026, 1, 1),
    valid_to: date = date(2027, 1, 1),
) -> Camera:
    return Camera(
        camera_id=camera_id,
        road_id=road_id,
        direction=direction,
        km_marker=km_marker,
        limit_kph=limit_kph,
        map_version=map_version,
        valid_from=valid_from,
        valid_to=valid_to,
        review_state=review_state,
    )


def window(
    window_id: str,
    road_id: str,
    direction: str,
    *,
    vehicle_id: str = "V-01",
    started_at: datetime | None = None,
    sample_rate_hz: float = 10,
    traffic_state: TrafficState = TrafficState.FREE,
    weather: str = "dry",
) -> TrajectoryWindow:
    return TrajectoryWindow(
        window_id=window_id,
        vehicle_id=vehicle_id,
        road_id=road_id,
        direction=direction,
        started_at=started_at or datetime(2026, 11, 2, 1, tzinfo=timezone.utc),
        sample_rate_hz=sample_rate_hz,
        traffic_state=traffic_state,
        weather=weather,
    )


def straight_run(
    window_id: str,
    *,
    speeds: tuple[float, float, float, float] = (60, 55, 50, 58),
    strategy_enabled: bool,
    camera_seen: bool = True,
    map_version: str = "M-1",
) -> tuple[SpeedSample, ...]:
    """四个样本：接近、识别、区内、离开；偏移固定便于构造对照。"""

    offsets = (0.0, 25.0, 50.0, 75.0)
    return tuple(
        SpeedSample(
            window_id=window_id,
            offset_m=offset,
            speed_kph=speed,
            strategy_enabled=strategy_enabled,
            camera_seen=camera_seen if index >= 1 else False,
            map_version=map_version,
        )
        for index, (offset, speed) in enumerate(zip(offsets, speeds))
    )


def plan(
    plan_id: str,
    road_id: str,
    direction: str,
    *,
    region: str = "R-S",
    version: str = "V15",
    status: PlanStatus = PlanStatus.ACCEPTED,
    effective_from: date = date(2026, 12, 2),
    receipt_id: str = "RC-1",
) -> RegionPlan:
    return RegionPlan(
        plan_id=plan_id,
        region=region,
        road_id=road_id,
        direction=direction,
        version=version,
        status=status,
        effective_from=effective_from,
        receipt_id=receipt_id,
    )


def materials(
    *,
    cameras: tuple[Camera, ...] = (),
    windows: tuple[TrajectoryWindow, ...] = (),
    samples: tuple[SpeedSample, ...] = (),
    plans: tuple[RegionPlan, ...] = (),
) -> Materials:
    return Materials(cameras=cameras, windows=windows, samples=samples, plans=plans)


def comparison_case(
    *,
    direction: str = "S",
    camera_direction: str = "S",
    strategy_runs: tuple[tuple[float, float, float, float], ...] = (
        (60, 55, 50, 58),
        (62, 56, 51, 59),
    ),
    control_runs: tuple[tuple[float, float, float, float], ...] = (
        (60, 58, 57, 59),
        (61, 59, 58, 60),
    ),
    extra_cameras: tuple[Camera, ...] = (),
) -> Materials:
    """构造一个道路/方向上有完整曝光、且策略与对照都存在的场景。

    两臂各默认两条经过，速度略有差异，使重采样区间不会退化。
    """

    cameras = (
        camera("C-A", "RD-A", camera_direction, limit_kph=50, map_version="M-1"),
        *extra_cameras,
    )
    windows: list[TrajectoryWindow] = []
    samples: list[SpeedSample] = []
    for index, speeds in enumerate(strategy_runs):
        window_id = f"WS-{index + 1:02d}"
        windows.append(window(window_id, "RD-A", direction, vehicle_id=f"V-S{index}"))
        samples.extend(
            straight_run(window_id, speeds=speeds, strategy_enabled=True)
        )
    for index, speeds in enumerate(control_runs):
        window_id = f"WC-{index + 1:02d}"
        windows.append(window(window_id, "RD-A", direction, vehicle_id=f"V-C{index}"))
        samples.extend(
            straight_run(window_id, speeds=speeds, strategy_enabled=False)
        )
    return materials(
        cameras=cameras,
        windows=tuple(windows),
        samples=tuple(samples),
        plans=(plan("P-A", "RD-A", direction),),
    )
