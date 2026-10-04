"""断联补账。

热量表断联期间，表头还在走字但总账收不到增量，月底结算必然少一截。补账按缺口段
处理：一段缺口有起止时刻、断联前后的表头读数、断联期间的流量温差采样与相邻时段
速率。两条口径在这里定死，调用方不能改：

* 方法按表型定——积算式表带流量温差通道，按流量温差反算；总量式表只有表头读数，
  先取前后读数差（回绕已接上），读数缺失才退到相邻时段速率插值；
* 分档按阈值定——估计量不超过全额阈值、缺口不超长、回绕不超过一次的缺口全额补；
  任一条件不满足整段标缺，估计值只作参考。绝不一律补满，缺条件的段宁可挂起也不
  硬凑一个数进总账。

补账结果只追加不改写：缺口估计、实际补账量与差异说明一起进补账流水，总账与审计
看到的是同一个数。
"""

from __future__ import annotations

from dataclasses import dataclass

from ..config import QuotaLimits
from .meter import INTEGRATING, TOTALIZING

WATER_CP_KJ_PER_KG_K = 4.186
SECONDS_PER_HOUR = 3600.0

METHOD_FLOW_TEMP = "flow_temp"
METHOD_INTERPOLATE = "interpolate"

STATUS_FILLED = "filled"
STATUS_MISSING = "missing"

# 采样覆盖容忍度：超过两个采样周期没有点，视为断联期间流量温差条件缺失。
COVERAGE_PERIODS = 2.0


def method_for_kind(kind: str) -> str:
    """按表型给出补账方法（积算式反算，总量式插值）。"""

    if kind == INTEGRATING:
        return METHOD_FLOW_TEMP
    if kind == TOTALIZING:
        return METHOD_INTERPOLATE
    raise ValueError(f"未知表型: {kind}")


def unwrap_delta(before_mwh: float, after_mwh: float, wrap_mwh: float) -> tuple[float, int]:
    """把回绕前后的表头读数接成实际增量，返回（增量，回绕次数）。"""

    before = float(before_mwh)
    after = float(after_mwh)
    wrap = float(wrap_mwh)
    if not 0.0 <= before < wrap or not 0.0 <= after < wrap:
        raise ValueError("表头读数必须落在 [0, 回绕阈值) 内")
    if after >= before:
        return after - before, 0
    return (wrap - before) + after, 1


@dataclass(frozen=True)
class FlowTempSample:
    """断联期间的一次流量温差采样。"""

    ts: float
    flow_t_h: float
    delta_t_c: float

    def power_mw(self) -> float:
        """该采样点的热功率；负流量或负温差按零计（热量表不倒走）。"""

        return (
            max(0.0, self.flow_t_h)
            * max(0.0, self.delta_t_c)
            * WATER_CP_KJ_PER_KG_K
            / SECONDS_PER_HOUR
        )


@dataclass(frozen=True)
class GapSegment:
    """一段待补的断联缺口（补账输入）。"""

    meter: str
    start_ts: float
    end_ts: float
    reading_before_mwh: float | None = None
    reading_after_mwh: float | None = None
    samples: tuple[FlowTempSample, ...] = ()
    rate_before_mwh_s: float | None = None
    rate_after_mwh_s: float | None = None
    collected_mwh: float = 0.0


def integrate_flow_temp(
    samples: tuple[FlowTempSample, ...], start_ts: float, end_ts: float, max_hole_s: float
) -> tuple[float, bool]:
    """按流量温差采样梯形积分出缺口热量，返回（热量，覆盖是否完整）。

    采样必须覆盖整个缺口：首尾样本距缺口边界不超过 ``max_hole_s``，相邻样本间隔
    也不超过 ``max_hole_s``；任一条件不满足即视为缺条件，调用方整段标缺。
    """

    points = sorted(
        (
            sample
            for sample in samples
            if start_ts - max_hole_s <= sample.ts <= end_ts + max_hole_s
        ),
        key=lambda sample: sample.ts,
    )
    if not points:
        return 0.0, False
    if points[0].ts - start_ts > max_hole_s or end_ts - points[-1].ts > max_hole_s:
        return 0.0, False
    series = [FlowTempSample(start_ts, points[0].flow_t_h, points[0].delta_t_c)]
    series.extend(point for point in points if start_ts < point.ts < end_ts)
    series.append(FlowTempSample(end_ts, points[-1].flow_t_h, points[-1].delta_t_c))
    energy_mwh = 0.0
    for left, right in zip(series, series[1:]):
        if right.ts - left.ts > max_hole_s:
            return 0.0, False
        energy_mwh += (
            0.5 * (left.power_mw() + right.power_mw()) * (right.ts - left.ts) / SECONDS_PER_HOUR
        )
    return energy_mwh, True


@dataclass(frozen=True)
class BackfillPlan:
    """一段缺口的补账结果（补账输出，落流水与审计）。"""

    meter: str
    method: str
    status: str
    start_ts: float
    end_ts: float
    estimate_mwh: float | None
    backfill_mwh: float
    collected_mwh: float
    reading_before_mwh: float | None
    reading_after_mwh: float | None
    wraps: int
    reasons: tuple[str, ...]
    difference: dict

    def as_dict(self) -> dict:
        return {
            "meter": self.meter,
            "method": self.method,
            "status": self.status,
            "start_ts": round(self.start_ts, 3),
            "end_ts": round(self.end_ts, 3),
            "estimate_mwh": None if self.estimate_mwh is None else round(self.estimate_mwh, 4),
            "backfill_mwh": round(self.backfill_mwh, 4),
            "collected_mwh": round(self.collected_mwh, 4),
            "reading_before_mwh": self.reading_before_mwh,
            "reading_after_mwh": self.reading_after_mwh,
            "wraps": self.wraps,
            "reasons": list(self.reasons),
            "difference": self.difference,
        }


def _difference_note(
    method: str,
    status: str,
    estimate_mwh: float | None,
    reading_gap_mwh: float | None,
    wraps: int,
    collected_mwh: float,
    backfill_mwh: float,
    reasons: tuple[str, ...],
) -> str:
    """生成人可读的差异说明，解释补账量与表差之间差在哪。"""

    if status == STATUS_MISSING:
        estimate_text = "未知" if estimate_mwh is None else f"{estimate_mwh:.4f} MWh"
        return f"整段标缺（{'、'.join(reasons)}）：估计 {estimate_text} 仅作参考，不入账"
    parts: list[str] = []
    if method == METHOD_FLOW_TEMP:
        parts.append(f"流量温差反算 {estimate_mwh:.4f} MWh")
        if reading_gap_mwh is not None:
            parts.append(f"表差 {reading_gap_mwh:.4f} MWh，口径差 {estimate_mwh - reading_gap_mwh:+.4f} MWh")
    elif reading_gap_mwh is not None:
        parts.append(f"读数差 {reading_gap_mwh:.4f} MWh（回绕 {wraps} 次已接上）")
    else:
        parts.append(f"相邻时段插值 {estimate_mwh:.4f} MWh")
    parts.append(f"断联期间已入账 {collected_mwh:.4f} MWh，本次补 {backfill_mwh:.4f} MWh")
    return "；".join(parts)


def plan_gap(segment: GapSegment, kind: str, limits: QuotaLimits) -> BackfillPlan:
    """对一段缺口出补账方案：方法按表型，分档按阈值，缺条件整段标缺。"""

    method = method_for_kind(kind)
    start_ts = float(segment.start_ts)
    end_ts = float(segment.end_ts)
    gap_s = end_ts - start_ts
    if gap_s <= 0:
        raise ValueError("缺口起止时刻非法")
    collected = float(segment.collected_mwh)
    if collected < 0:
        raise ValueError("断联期间已入账热量不能为负")

    reading_gap: float | None = None
    wraps = 0
    if segment.reading_before_mwh is not None and segment.reading_after_mwh is not None:
        reading_gap, wraps = unwrap_delta(
            segment.reading_before_mwh, segment.reading_after_mwh, limits.meter_wrap_mwh
        )

    estimate: float | None = None
    reasons: list[str] = []
    if method == METHOD_FLOW_TEMP:
        estimate, covered = integrate_flow_temp(
            segment.samples, start_ts, end_ts, COVERAGE_PERIODS * limits.sample_period_s
        )
        if not covered:
            estimate = None
            reasons.append("samples_incomplete")
    elif reading_gap is not None:
        estimate = reading_gap
    elif segment.rate_before_mwh_s is not None and segment.rate_after_mwh_s is not None:
        rate = 0.5 * (float(segment.rate_before_mwh_s) + float(segment.rate_after_mwh_s))
        if rate < 0:
            raise ValueError("相邻时段速率不能为负")
        estimate = rate * gap_s
    else:
        reasons.append("readings_missing")

    if estimate is not None:
        if gap_s > limits.backfill_max_gap_s:
            reasons.append("gap_too_long")
        if wraps > 1:
            reasons.append("wraps_ambiguous")
        if estimate > limits.backfill_full_below_mwh:
            reasons.append("over_full_threshold")

    status = STATUS_MISSING if reasons else STATUS_FILLED
    owed = None if estimate is None else max(0.0, estimate - collected)
    backfill = owed if status == STATUS_FILLED else 0.0
    unresolved = owed if status == STATUS_MISSING else 0.0
    difference = {
        "reading_gap_mwh": None if reading_gap is None else round(reading_gap, 4),
        "estimate_mwh": None if estimate is None else round(estimate, 4),
        "collected_mwh": round(collected, 4),
        "backfill_mwh": round(backfill, 4),
        "unresolved_mwh": None if unresolved is None else round(unresolved, 4),
        "note": _difference_note(
            method, status, estimate, reading_gap, wraps, collected, backfill, tuple(reasons)
        ),
    }
    return BackfillPlan(
        meter=segment.meter,
        method=method,
        status=status,
        start_ts=start_ts,
        end_ts=end_ts,
        estimate_mwh=estimate,
        backfill_mwh=backfill,
        collected_mwh=collected,
        reading_before_mwh=segment.reading_before_mwh,
        reading_after_mwh=segment.reading_after_mwh,
        wraps=wraps,
        reasons=tuple(reasons),
        difference=difference,
    )


def summarize_entries(entries: list[dict]) -> dict:
    """把补账流水折成汇总计数，供控制台与配额报告共用。"""

    filled = [entry for entry in entries if entry.get("status") == STATUS_FILLED]
    missing = [entry for entry in entries if entry.get("status") == STATUS_MISSING]
    by_method: dict[str, int] = {}
    by_meter: dict[str, dict] = {}
    for entry in entries:
        method = str(entry.get("method", ""))
        by_method[method] = by_method.get(method, 0) + 1
        slot = by_meter.setdefault(
            str(entry.get("meter", "")), {"filled": 0, "missing": 0, "backfilled_mwh": 0.0}
        )
        slot["filled" if entry.get("status") == STATUS_FILLED else "missing"] += 1
        slot["backfilled_mwh"] += float(entry.get("backfill_mwh", 0.0))
    unresolved = 0.0
    for entry in missing:
        value = entry.get("difference", {}).get("unresolved_mwh")
        if value is not None:
            unresolved += float(value)
    return {
        "total": len(entries),
        "filled": len(filled),
        "missing": len(missing),
        "backfilled_mwh": round(sum(float(entry.get("backfill_mwh", 0.0)) for entry in filled), 4),
        "unresolved_mwh": round(unresolved, 4),
        "by_method": by_method,
        "by_meter": {
            name: {**slot, "backfilled_mwh": round(slot["backfilled_mwh"], 4)}
            for name, slot in sorted(by_meter.items())
        },
    }
