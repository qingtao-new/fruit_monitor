"""阻抗谱组装与绘图数据提取。

节点端的 AD5933 频扫耗时长，按分包 ``impedance_raw`` 独立发送，
本模块在 PC 侧把分包还原成完整谱，并产出 Nyquist / Bode 直接可用的
绘图序列。所有函数都是纯函数或无副作用的组装器，便于单测。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

import math

from protocol import (
    ImpedanceRawPacket,
    ImpedanceSpectrum,
    SpectrumPoint,
    SweepPointData,
    validate_impedance_raw_packet,
)


@dataclass
class _ScanBucket:
    points: dict[int, tuple[float, float, float]] = field(default_factory=dict)
    timestamp: int = 0
    packet_total: int = 0
    max_packet_index: int = -1
    seen_packets: int = 0
    expected_points: int = 0
    order: int = 0


class SpectrumAssembler:
    """把 ``impedance_raw`` 分包拼装为完整 :class:`ImpedanceSpectrum`。

    完成判定有两条，任一满足即产出结果：
    1. 期望频点数收齐（``expected_points == len(points)``）
    2. 收到最后一个分包（``packet_index == packet_total - 1``）

    第 2 条保证丢包时仍能出一张图，而不是永远等不到；
    缺失频点直接跳过，``missing`` 元数据里记录缺口数量。

    ``feed`` 无状态地按 ``scan_id`` 分桶，天然支持多节点并发扫频。
    """

    def __init__(self, scan_timeout_s: float = 180.0, max_scans: int = 32) -> None:
        self._buckets: dict[tuple[str, str, int], _ScanBucket] = {}
        self._scan_timeout_s = float(scan_timeout_s)
        self._max_scans = int(max_scans)
        self._order: int = 0
        self.completed: int = 0
        self.bad_packets: int = 0
        self.lenient_packets: int = 0

    def _key(self, pkt: ImpedanceRawPacket) -> tuple[str, str, int]:
        return (pkt.gateway_id, pkt.node_id, pkt.scan_id)

    def feed(self, pkt: ImpedanceRawPacket, now_ts: Optional[int] = None) -> Optional[ImpedanceSpectrum]:
        ok, flags = validate_impedance_raw_packet(pkt)
        if not pkt.points or "points_len_mismatch" in flags:
            self.bad_packets += 1
            return None
        if not ok:
            self.lenient_packets += 1

        self._expire(now_ts)

        key = self._key(pkt)
        bucket = self._buckets.get(key)
        if bucket is None:
            bucket = _ScanBucket()
            bucket.order = self._order
            self._order += 1
            self._buckets[key] = bucket

        bucket.timestamp = pkt.timestamp or bucket.timestamp
        bucket.packet_total = max(bucket.packet_total, pkt.packet_total)
        bucket.seen_packets += 1
        bucket.max_packet_index = max(bucket.max_packet_index, pkt.packet_index)
        bucket.expected_points = max(
            bucket.expected_points, pkt.point_start + pkt.point_count
        )

        for offset, point in enumerate(pkt.points):
            idx = pkt.point_start + offset
            bucket.points[idx] = (pkt.frequency_at(offset), float(point.re), float(point.im))

        complete = (
            bucket.expected_points > 0
            and len(bucket.points) >= bucket.expected_points
            and bucket.max_packet_index >= bucket.packet_total - 1
        )
        if not complete:
            self._expire(now_ts)
            return None

        self._buckets.pop(key, None)
        self.completed += 1
        total_expected = bucket.expected_points
        got = sorted(bucket.points.items())
        missing = total_expected - len(got) if total_expected > len(got) else 0

        spectrum = ImpedanceSpectrum(
            gateway_id=pkt.gateway_id,
            node_id=pkt.node_id,
            scan_id=pkt.scan_id,
            timestamp=bucket.timestamp or pkt.timestamp,
            points=[
                SpectrumPoint(
                    frequency_hz=freq,
                    z_real=re,
                    z_imag=im,
                    magnitude=math.hypot(re, im),
                    phase_deg=math.degrees(math.atan2(-im, re)),
                )
                for _idx, (freq, re, im) in got
            ],
            meta={
                "packets": bucket.seen_packets,
                "packet_total": bucket.packet_total,
                "points_expected": total_expected,
                "points_missing": missing,
            },
        )
        self._expire(now_ts)
        return spectrum.sort_by_frequency()

    def _expire(self, now_ts: Optional[int]) -> None:
        if not self._buckets:
            return
        if now_ts is not None:
            stale = [
                key
                for key, bucket in self._buckets.items()
                if bucket.timestamp > 1e8
                and now_ts - bucket.timestamp > self._scan_timeout_s
            ]
            for key in stale:
                self._buckets.pop(key, None)
        while len(self._buckets) > self._max_scans:
            oldest = min(self._buckets.items(), key=lambda kv: (kv[1].timestamp, kv[1].order))
            self._buckets.pop(oldest[0], None)

    def pending(self) -> int:
        return len(self._buckets)

    def reset(self) -> None:
        self._buckets.clear()

    @staticmethod
    def to_sweep_points(
        spectrum: ImpedanceSpectrum,
        round_id: Optional[int] = None,
        context: Optional[dict[str, float]] = None,
    ) -> list[SweepPointData]:
        return to_sweep_points(spectrum, round_id=round_id, context=context)


def to_sweep_points(
    spectrum: ImpedanceSpectrum,
    round_id: Optional[int] = None,
    context: Optional[dict[str, float]] = None,
) -> list[SweepPointData]:
    """把整谱转成落库点列，附带该轮的传感器上下文快照。"""
    context = context or {}
    return [
        SweepPointData(
            gateway_id=spectrum.gateway_id,
            node_id=spectrum.node_id,
            round_id=int(round_id if round_id is not None else spectrum.scan_id),
            timestamp=spectrum.timestamp,
            point_index=i,
            frequency_hz=point.frequency_hz,
            z_real=point.z_real,
            z_imag=point.z_imag,
            magnitude=point.magnitude,
            soil_moisture=context.get("soil_moisture"),
            temperature=context.get("temperature"),
            nh3=context.get("nh3"),
            h2s=context.get("h2s"),
            co2=context.get("co2"),
            ph=context.get("ph"),
            humidity=context.get("humidity"),
            report_id=f"{spectrum.gateway_id}/{spectrum.node_id}/R{spectrum.scan_id}",
        )
        for i, point in enumerate(spectrum.points)
    ]


def nyquist_series(
    spectrum: ImpedanceSpectrum,
    min_frequency_hz: float = 0.0,
) -> dict[str, list[float]]:
    """Nyquist 轨迹序列：x=Z'，y=-Z''，按频率升序即时间前进方向。"""
    pts = [p for p in spectrum.points if p.frequency_hz >= min_frequency_hz]
    return {
        "x": [p.z_real for p in pts],
        "y": [-p.z_imag for p in pts],
        "frequency": [p.frequency_hz for p in pts],
    }


def bode_series(
    spectrum: ImpedanceSpectrum,
    min_frequency_hz: float = 0.0,
) -> dict[str, list[float]]:
    """Bode 序列：对数频率轴上的 |Z| 与相位。"""
    pts = [p for p in spectrum.points if p.frequency_hz >= min_frequency_hz]
    magnitudes = [p.magnitude if p.magnitude is not None else math.hypot(p.z_real, p.z_imag) for p in pts]
    phases = [
        p.phase_deg
        if p.phase_deg is not None
        else math.degrees(math.atan2(-p.z_imag, p.z_real))
        for p in pts
    ]
    return {
        "frequency": [p.frequency_hz for p in pts],
        "magnitude": magnitudes,
        "phase": phases,
    }


def characteristic_frequency(
    spectrum: ImpedanceSpectrum,
    lo_hz: float = 0.0,
    hi_hz: float = float("inf"),
) -> Optional[float]:
    """弛豫特征频率 ``f_c``：Nyquist 圆弧顶点（``-Z''`` 最大处）对应的频率。

    对单极化弧等价于 Cole-Cole 的弛豫时间倒数 ``1/(2πτ)``，
    是电极/组织界面的固有弛豫位置。取圆弧顶点而不是相位峰值，
    是因为实测谱的相位在扫频段内往往单调上升、峰值落在带边，
    那个位置没有物理意义。
    """
    best_freq: Optional[float] = None
    best_abs = 0.0
    for point in spectrum.points:
        if not (lo_hz <= point.frequency_hz <= hi_hz):
            continue
        imag = abs(point.z_imag)
        if imag > best_abs:
            best_abs = imag
            best_freq = point.frequency_hz
    return best_freq


def sweep_points_to_spectrum(points: list[SweepPointData]) -> ImpedanceSpectrum:
    """把散落的扫频点还原成整谱对象。

    网关按段上报扫频，PC 侧不知道总点数，因此按"该轮收到点后静默一段时间"
    判定一轮结束，再统一还原成 :class:`ImpedanceSpectrum`，
    让落库点、Nyquist / Bode 绘图和成熟度估计共用同一份数据结构。
    """
    usable = [
        p
        for p in points
        if p.frequency_hz and p.z_real is not None and p.z_imag is not None
    ]
    usable.sort(key=lambda p: (p.frequency_hz or 0.0, p.point_index or 0))
    gateway_id = next((p.gateway_id for p in usable), "")
    node_id = next((p.node_id for p in usable), "")
    round_id = next((p.round_id for p in usable), 0)
    timestamp = max((p.timestamp for p in usable), default=0)
    return ImpedanceSpectrum(
        gateway_id=gateway_id,
        node_id=node_id,
        scan_id=int(round_id or 0),
        timestamp=timestamp,
        points=[
            SpectrumPoint(
                frequency_hz=float(p.frequency_hz),
                z_real=float(p.z_real),
                z_imag=float(p.z_imag),
                magnitude=p.magnitude,
                phase_deg=math.degrees(math.atan2(-float(p.z_imag), float(p.z_real))),
            )
            for p in usable
        ],
    ).sort_by_frequency()


def window_stats(
    spectrum: ImpedanceSpectrum,
    lo_hz: float = 1000.0,
    hi_hz: float = 30000.0,
) -> dict[str, float]:
    """有效频段内的 |Z| 统计量。

    低频段受极化效应主导、高频段受引线电感和电容分路主导，
    只有中段能真实反映果肉组织阻抗，所以特征提取默认限定该窗口。
    """
    values = [
        p.magnitude if p.magnitude is not None else math.hypot(p.z_real, p.z_imag)
        for p in spectrum.points
        if lo_hz <= p.frequency_hz <= hi_hz
    ]
    if not values:
        return {"count": 0.0, "mean": 0.0, "std": 0.0, "min": 0.0, "max": 0.0}
    mean = sum(values) / len(values)
    variance = sum((v - mean) ** 2 for v in values) / len(values)
    return {
        "count": float(len(values)),
        "mean": mean,
        "std": math.sqrt(variance),
        "min": min(values),
        "max": max(values),
    }
