"""断联补账引擎。

输入是只读的原始采样（:class:`~heatnet.recon.state.Sample`）和按表登记的表型
参数（:class:`~heatnet.recon.state.MeterProfile`），输出是不可变的补账结果
（:class:`~heatnet.recon.state.ReconciliationRun`）。处理分五步：

1. 网格化：按结算月窗口和表的采集周期把采样落到槽位，同槽重复采样记重复计数。
2. 回绕解绕：沿时间轴枚举 ``raw + n*wrap`` 候选，用最大瞬时功率限幅过滤：
   唯一候选才采纳并接上连续读数；0 个候选隔离为坏点；多于 1 个候选判定回绕
   多解，断链标缺，绝不猜一个。
3. 分段：相邻两个可用读数锚点之间为一个断联段；月窗边缘没有另一侧锚点的槽
   记 ``NO_ANCHOR``，不补。
4. 分档补算（口径见 :mod:`heatnet.recon.state` 的模块说明）：

   - rate 表：两端流量/温差反算功率，梯形积分；与寄存器读数差对账，残差超
     容差标缺；
   - total 表：短/中档用解绕后的两端读数差线性分配；读数冻住且只在短档时，
     才按相邻时段增量插值，邻居不够就标缺；长档一律标缺，读数差只挂账披露。
5. 总账恒等式核对：``registered == observed + filled + unbooked``，不成立直接
   抛 :class:`ConsistencyError`——这是程序错误，不能带着不一致的账落审计。
"""

from __future__ import annotations

import calendar
import hashlib
import math
from dataclasses import dataclass

from .state import (
    AMBIGUOUS_ROLLOVER,
    BAD_SAMPLE,
    FILLED_REASONS,
    MISSING_CONDITION,
    MISSING_LONG,
    MISSING_NEIGHBOR,
    MISSING_REASONS,
    NO_ANCHOR,
    Policy,
    RATE_CLOSURE_MISMATCH,
    RATE_TRAPEZOID,
    Sample,
    Segment,
    BadSlot,
    MeterProfile,
    MeterResult,
    ReconciliationRun,
    TIER_LONG,
    TIER_MID,
    TIER_SHORT,
    TOTAL_NEIGHBOR_INTERP,
    TOTAL_REGISTER_ALLOC,
)

_EPS = 1e-9


class ConsistencyError(RuntimeError):
    """补账、总账之间的恒等式不成立。属于程序错误，必须中断而不是掩盖。"""


class ReconcileConfigError(ValueError):
    """表型参数或输入口径非法。"""


@dataclass
class _Point:
    slot: int
    ts: float
    raw: float
    unwrapped: float | None = None
    chain: int = -1
    power: float | None = None      # 分量反算的瞬时功率（kW）
    rate_usable: bool = False      # 分量是否够反算
    ambiguous: bool = False        # 回绕多解：保留为链边界但不可量化读数差


def month_window(month: str) -> tuple[float, float]:
    """把 ``YYYY-MM`` 转成 UTC 月窗 ``[start, end)`` 的秒级时间戳。"""

    try:
        year_s, month_s = month.split("-")
        year, mon = int(year_s), int(month_s)
    except (ValueError, AttributeError) as error:
        raise ReconcileConfigError(f"月份必须是 YYYY-MM 格式: {month!r}") from error
    if not 1 <= mon <= 12:
        raise ReconcileConfigError(f"月份越界: {month!r}")
    start = calendar.timegm((year, mon, 1, 0, 0, 0, 0, 0, 0))
    last_day = calendar.monthrange(year, mon)[1]
    if mon == 12:
        end = calendar.timegm((year + 1, 1, 1, 0, 0, 0, 0, 0, 0))
    else:
        end = calendar.timegm((year, mon + 1, 1, 0, 0, 0, 0, 0, 0))
    return float(start), float(end)


def rate_power_kw(sample: Sample, profile: MeterProfile) -> float | None:
    """按 流量×温差 反算瞬时功率（kW）。任一条件缺失返回 ``None``。"""

    if sample.flow_t_h is None or sample.supply_c is None or sample.return_c is None:
        return None
    dt_c = sample.supply_c - sample.return_c
    if sample.flow_t_h < 0 or dt_c < 0:
        return None
    return profile.k_factor * sample.flow_t_h * dt_c


def _tier(missing_slots: int, duration_h: float, policy: Policy) -> str:
    if missing_slots <= policy.short_slots:
        return TIER_SHORT
    if duration_h <= policy.max_gap_hours + _EPS:
        return TIER_MID
    return TIER_LONG


def _allocate_trapezoid(missing: int, dt_h: float, p0: float, p1: float) -> tuple[float, ...]:
    """把锚点间梯形积分总量分配到各缺失槽（首末槽各承担一个半区间，总量闭合）。

    瞬时功率在断联窗内按两端线性变化；槽 j 分到的量 = 相邻两个半区间之和，
    各槽相加恰好等于整段梯形积分，不丢不重。
    """

    instants = missing + 1
    intervals = [
        dt_h * (p0 + (p1 - p0) * j / instants + p0 + (p1 - p0) * (j + 1) / instants) / 2.0
        for j in range(instants)
    ]
    if missing == 1:
        return (sum(intervals),)
    allocated: list[float] = []
    for index in range(missing):
        if index == 0:
            value = intervals[0] + intervals[1] / 2.0
        elif index == missing - 1:
            value = intervals[-2] / 2.0 + intervals[-1]
        else:
            value = (intervals[index] + intervals[index + 1]) / 2.0
        allocated.append(value)
    return tuple(allocated)


def _neighbor_increments(
    points: list[_Point], left_anchor_index: int, right_anchor_index: int
) -> tuple[float | None, float | None]:
    """取断联段两端外侧"相邻时段"的每槽增量（kWh/槽）。

    左邻居是左锚点与其前一个同链好点之间的增量，右邻居是右锚点与后一个同链
    好点之间的增量；跨链、坏点都不算合格邻居。
    """

    left = right = None
    if left_anchor_index - 1 >= 0 and points[left_anchor_index - 1].chain == points[left_anchor_index].chain:
        prev_point = points[left_anchor_index - 1]
        span = points[left_anchor_index].slot - prev_point.slot
        left = (points[left_anchor_index].unwrapped - prev_point.unwrapped) / span
    if (
        right_anchor_index + 1 < len(points)
        and points[right_anchor_index + 1].chain == points[right_anchor_index].chain
    ):
        next_point = points[right_anchor_index + 1]
        span = next_point.slot - points[right_anchor_index].slot
        right = (next_point.unwrapped - points[right_anchor_index].unwrapped) / span
    return left, right


def reconcile_meter(
    profile: MeterProfile,
    samples: list[Sample],
    window_start: float,
    window_end: float,
    policy: Policy,
) -> MeterResult:
    """对一块表执行完整补账。原始采样不会被修改。"""

    if profile.kind not in ("rate", "total"):
        raise ReconcileConfigError(f"未知表型: {profile.kind!r}（表 {profile.meter}）")
    if profile.wrap_kwh <= 0 or profile.period_s <= 0:
        raise ReconcileConfigError(f"回绕量程与采集周期必须为正: {profile.meter}")

    period = float(profile.period_s)
    window = window_end - window_start
    if window % period > _EPS:
        raise ReconcileConfigError(
            f"采集周期 {period}s 不能整除结算月窗，表 {profile.meter} 无法对齐槽位"
        )
    n_slots = int(round(window / period))
    dt_h = period / 3600.0

    # 1) 网格化：同槽多条采样以最后一条为准；原始重复导入在服务层归档留痕。
    grid: dict[int, Sample] = {}
    for sample in samples:
        if sample.meter != profile.meter or not (window_start <= sample.ts < window_end):
            continue
        offset = sample.ts - window_start
        slot = int(math.floor(offset / period + _EPS))
        if slot >= n_slots:
            continue
        grid[slot] = sample

    present = sorted(grid)
    points: list[_Point] = []
    for slot in present:
        sample = grid[slot]
        if sample.energy_kwh is None:
            # 没有累计读数就当不了锚点；分量仍可能在反算里用，这里不丢采样事实。
            points.append(
                _Point(slot, sample.ts, math.nan, unwrapped=None,
                       power=rate_power_kw(sample, profile))
            )
            continue
        power = rate_power_kw(sample, profile)
        points.append(
            _Point(
                slot,
                sample.ts,
                float(sample.energy_kwh),
                power=power,
                rate_usable=power is not None
                and (profile.kind != "rate" or (sample.flow_t_h is not None)),
            )
        )

    # 2) 回绕解绕。
    #    有功率上限：枚举所有不超过限幅窗口的候选，唯一才采纳；0 个 = 跳变坏点；
    #    多个 = 回绕次数多解，断链标缺，且后续点相对多解点（新链基线）重新解绕。
    #    无功率上限：按"累计热量不减"取最小非负增量（标准物理解绕），此时无法
    #    检测多解（多解本身就要求有功率限幅证据）。
    register_points = [point for point in points if not math.isnan(point.raw)]
    bad_slots: list[BadSlot] = []
    chain_no = 0
    # ``last_good`` 为 None 表示下一个读数点是新链基线；多解点之后同样置 None。
    last_good: _Point | None = None
    for point in register_points:
        if last_good is None:
            point.unwrapped = point.raw % profile.wrap_kwh
            point.chain = chain_no
            last_good = point
            continue
        slot_span = point.slot - last_good.slot
        if profile.max_power_kw is None:
            n_min = max(0, math.ceil((last_good.unwrapped - point.raw - _EPS) / profile.wrap_kwh))
            candidate = point.raw + n_min * profile.wrap_kwh
            if candidate - last_good.unwrapped < -_EPS:
                bad_slots.append(BadSlot(profile.meter, point.ts, BAD_SAMPLE, point.raw))
                # 坏点不推进链条：后续点仍相对上一好点解绕。
                continue
            point.unwrapped = candidate
            point.chain = last_good.chain
            last_good = point
            continue

        cap = profile.max_power_kw * slot_span * dt_h
        candidates: list[float] = []
        n = 0
        while True:
            candidate = point.raw + n * profile.wrap_kwh
            delta = candidate - last_good.unwrapped
            if delta > cap + _EPS:
                break
            if delta >= -_EPS:
                candidates.append(candidate)
            n += 1
            if n > 1_000_000:  # 配置异常保护：回绕量程相对功率小到不合理
                raise ReconcileConfigError(
                    f"表 {profile.meter} 回绕候选超过上限，检查 wrap_kwh/max_power_kw"
                )
        if len(candidates) == 1:
            point.unwrapped = candidates[0]
            point.chain = last_good.chain
            last_good = point
        elif len(candidates) == 0:
            # 读数跳变越限：隔离该槽，链条不推进；后续点相对上一好点继续解绕。
            bad_slots.append(BadSlot(profile.meter, point.ts, BAD_SAMPLE, point.raw))
        else:
            # 回绕次数多解：该点作为新链基线保留（缺口段由此点两侧承载并标缺），
            # 但它与旧链之间读数差不可量化，绝不猜一个回绕次数；下一个点相对
            # 这个新基线重新枚举。
            chain_no += 1
            point.unwrapped = point.raw % profile.wrap_kwh
            point.chain = chain_no
            point.ambiguous = True
            bad_slots.append(BadSlot(profile.meter, point.ts, AMBIGUOUS_ROLLOVER, point.raw))
            last_good = point

    # 锚点 = 解绕成功的点（含多解边界点）；坏点不进锚点序列。
    good = [point for point in register_points if point.unwrapped is not None and point.chain >= 0]

    # 月窗边缘无锚点的槽：标缺但不成段（月初第一条读数之前 / 月末最后一条之后）。
    no_anchor_slots = 0
    edge_status: dict[int, str] = {}
    first_slot = good[0].slot if good else n_slots
    last_slot = good[-1].slot if good else -1
    for slot in range(n_slots):
        if slot < first_slot or slot > last_slot:
            if slot not in grid:
                no_anchor_slots += 1
                edge_status[slot] = NO_ANCHOR

    # 3)+4) 逐段分档补算。
    segments: list[Segment] = []
    anchors: list[tuple[float, float, float]] = []
    observed = filled_total = registered = missing_gap_total = 0.0
    residual_total = correction_total = 0.0
    ambiguous_breaks = 0
    filled_segments = missing_segments = 0

    for index in range(len(good)):
        point = good[index]
        anchors.append((point.ts, point.raw, point.unwrapped))

    for index in range(1, len(good)):
        before, after = good[index - 1], good[index]
        missing = after.slot - before.slot - 1
        same_chain = before.chain == after.chain
        duration_h = (missing + 1) * dt_h

        if not same_chain:
            ambiguous_breaks += 1

        if missing == 0 and same_chain:
            dreg = after.unwrapped - before.unwrapped
            observed += dreg
            registered += dreg
            continue
        if missing == 0 and not same_chain:
            # 理论上不会发生：多解点本身占槽，相邻槽不可能直接跨链。防御性处理。
            raise ConsistencyError("跨链锚点相邻出现，解绕链构造有误")

        dreg = after.unwrapped - before.unwrapped
        tier = _tier(missing, duration_h, policy)
        segment: Segment

        if not same_chain:
            # 跨链边的两个连续读数基准之间没有确定的回绕关系，读数差不可量化：
            # registered 只累加同链边，这段不产生任何能量账，只披露时长与原因。
            segment = Segment(
                profile.meter, before.ts, after.ts, missing, tier,
                AMBIGUOUS_ROLLOVER, None, None,
                register_delta_kwh=None,
                anchor_before_kwh=before.unwrapped,
                anchor_after_kwh=after.unwrapped,
                raw_before_kwh=before.raw, raw_after_kwh=after.raw,
                power_before_kw=before.power, power_after_kw=after.power,
            )
        elif profile.kind == "rate":
            segment = _fill_rate(
                profile, policy, before, after, missing, dt_h, dreg, tier
            )
        else:
            segment = _fill_total(
                profile, policy, good, index, before, after, missing, dt_h, dreg, tier
            )

        segments.append(segment)
        if same_chain:
            registered += dreg
        # 总账恒等式在逐段累加上维护。
        if segment.is_filled():
            value = float(segment.filled_kwh)
            filled_total += value
            if profile.kind == "rate" and segment.residual_kwh is not None:
                residual_total += segment.residual_kwh
            if segment.reason == TOTAL_NEIGHBOR_INTERP:
                correction_total += value  # 读数差为 0 的表计修正
            filled_segments += 1
        else:
            missing_segments += 1
            if same_chain and segment.register_delta_kwh is not None:
                # 标缺段的读数差只披露、不入账（长档挂账 / 对账失败 / 缺条件等）。
                missing_gap_total += segment.register_delta_kwh

    # 坏槽补登：缺采样槽由所属段携带；无锚点边槽已在上面计数；坏点/多解点已登记。
    result = MeterResult(
        meter=profile.meter,
        kind=profile.kind,
        wrap_kwh=profile.wrap_kwh,
        period_s=profile.period_s,
        segments=tuple(segments),
        bad_slots=tuple(bad_slots)
        + tuple(
            BadSlot(profile.meter, window_start + slot * period, NO_ANCHOR)
            for slot in edge_status
        ),
        anchors=tuple(anchors),
        observed_kwh=observed,
        filled_kwh=filled_total,
        registered_kwh=registered,
        missing_gap_kwh=missing_gap_total,
        residual_kwh=residual_total,
        correction_kwh=correction_total,
        missing_segments=missing_segments,
        filled_segments=filled_segments,
        ambiguous_breaks=ambiguous_breaks,
        no_anchor_slots=no_anchor_slots,
        max_power_kw=profile.max_power_kw,
    )
    _assert_meter_identity(result)
    return result


def _fill_rate(
    profile: MeterProfile,
    policy: Policy,
    before: _Point,
    after: _Point,
    missing: int,
    dt_h: float,
    dreg: float,
    tier: str,
) -> Segment:
    """rate 表：流量温差反算梯形积分；与寄存器读数差对账。"""

    common = dict(
        meter=profile.meter,
        start_ts=before.ts,
        end_ts=after.ts,
        missing_slots=missing,
        register_delta_kwh=dreg,
        anchor_before_kwh=before.unwrapped,
        anchor_after_kwh=after.unwrapped,
        raw_before_kwh=before.raw,
        raw_after_kwh=after.raw,
        wraps=int(round((dreg - (after.raw - before.raw)) / profile.wrap_kwh)),
        power_before_kw=before.power,
        power_after_kw=after.power,
    )

    if tier == TIER_LONG:
        return Segment(**common, tier=tier, reason=MISSING_LONG, filled_kwh=None, method=None)

    if not before.rate_usable or not after.rate_usable:
        # 断联边界缺流量/温差：缺条件的一段宁可标缺，不许硬凑。
        return Segment(**common, tier=tier, reason=MISSING_CONDITION, filled_kwh=None, method=None)

    # rate 段入账取寄存器权威读数差（反算只决定逐槽形状并负责对账），保证
    # 入账量与寄存器恒等；冻住插值是表计修正（读数差为 0、补量来自旁证）。
    slot_values = _allocate_trapezoid(missing, dt_h, before.power, after.power)
    raw_value = sum(slot_values)
    residual = dreg - raw_value
    scale = max(abs(dreg), abs(raw_value), 1e-9)
    if abs(residual) / scale > policy.closure_tolerance:
        # 反算量和寄存器读数差对不上：分量或读数有问题，整段标缺。
        return Segment(
            **common, tier=tier, reason=RATE_CLOSURE_MISMATCH, filled_kwh=None, method=None,
            residual_kwh=residual,
            slot_kwh=tuple(None for _ in slot_values),
        )
    if abs(raw_value) > _EPS:
        slot_values = tuple(value * dreg / raw_value for value in slot_values)
    return Segment(
        **common, tier=tier, reason=RATE_TRAPEZOID, filled_kwh=dreg,
        method="flow*dt trapezoid", residual_kwh=residual, slot_kwh=slot_values,
    )


def _fill_total(
    profile: MeterProfile,
    policy: Policy,
    good: list[_Point],
    index: int,
    before: _Point,
    after: _Point,
    missing: int,
    dt_h: float,
    dreg: float,
    tier: str,
) -> Segment:
    """total 表：读数差线性分配；短档冻住才允许邻居插值。"""

    common = dict(
        meter=profile.meter,
        start_ts=before.ts,
        end_ts=after.ts,
        missing_slots=missing,
        register_delta_kwh=dreg,
        anchor_before_kwh=before.unwrapped,
        anchor_after_kwh=after.unwrapped,
        raw_before_kwh=before.raw,
        raw_after_kwh=after.raw,
        wraps=int(round((dreg - (after.raw - before.raw)) / profile.wrap_kwh)),
        power_before_kw=before.power,
        power_after_kw=after.power,
    )

    if tier == TIER_LONG:
        # 定死：长档不做"回绕前一律补满"。读数差随差异说明挂账，不进账。
        return Segment(**common, tier=tier, reason=MISSING_LONG, filled_kwh=None, method=None)

    # 读数冻住判定：两端分量（若有）表明仍在用热，但寄存器增量为 0。
    powers = [point.power for point in (before, after) if point.power is not None]
    implied = (sum(powers) / len(powers) * dt_h) if powers else 0.0
    frozen = abs(dreg) <= _EPS and implied > policy.frozen_kwh_per_slot

    if not frozen and abs(dreg) > _EPS:
        # 有读数差就是最硬的物理依据：短档、中档都按读数差线性分配（已解绕）。
        # 缺几个采样槽就把读数差均摊到几个槽（锚点读数本身不重算），逐槽相加
        # 恰好等于读数差，不丢不重；锚点两侧的边界区间并入相邻缺失槽。
        per_slot = dreg / missing
        slot_values = tuple(per_slot for _ in range(missing))
        return Segment(
            **common, tier=tier, reason=TOTAL_REGISTER_ALLOC, filled_kwh=dreg,
            method="unwrapped register delta linear alloc",
            residual_kwh=0.0, slot_kwh=slot_values,
        )

    # 读数冻住（或读数差为 0 又没有旁证）：只有短档才允许相邻时段插值，
    # 且要求断联两侧各有一个紧邻的同链好点做邻居。
    if tier != TIER_SHORT:
        return Segment(**common, tier=tier, reason=MISSING_CONDITION, filled_kwh=None, method=None)

    left_inc, right_inc = _neighbor_increments(good, index - 1, index)
    neighbors = [value for value in (left_inc, right_inc) if value is not None]
    if left_inc is None or right_inc is None or not neighbors:
        return Segment(**common, tier=tier, reason=MISSING_NEIGHBOR, filled_kwh=None, method=None)

    per_slot = sum(neighbors) / len(neighbors)
    slot_values = tuple(per_slot for _ in range(missing))
    return Segment(
        **common, tier=tier, reason=TOTAL_NEIGHBOR_INTERP,
        filled_kwh=per_slot * missing,
        method="adjacent-period mean increment interp",
        residual_kwh=None, slot_kwh=slot_values,
    )


def _assert_meter_identity(result: MeterResult) -> None:
    """逐表恒等式：寄存器读数差 = 实读 + 读数差类补量 + 标缺段披露的读数差。

    冻住插值是表计修正（寄存器读数差为 0），单独入账为 correction，不参与
    "寄存器读数差"这条恒等式；rate 反算残差只披露，不改变入账量（入账取
    寄存器权威差）。
    """

    fills_from_register = sum(
        (segment.register_delta_kwh or 0.0)
        for segment in result.segments
        if segment.is_filled() and segment.register_delta_kwh is not None
    )
    disclosed = sum(
        segment.register_delta_kwh or 0.0
        for segment in result.segments
        if not segment.is_filled() and segment.register_delta_kwh is not None
    )
    expected_registered = result.observed_kwh + fills_from_register + disclosed
    if abs(expected_registered - result.registered_kwh) > 1e-6:
        raise ConsistencyError(
            f"表 {result.meter} 总账不闭合: 读数差合计 {result.registered_kwh:.6f} "
            f"!= 实读+补量+披露 {expected_registered:.6f}"
        )


def reconcile(
    month: str,
    profiles: list[MeterProfile],
    samples: list[Sample],
    policy: Policy = Policy(),
    *,
    created_ts: float,
    run_id: str,
) -> ReconciliationRun:
    """对一个结算月执行整批补账。结果不可变；原始采样只被读取。"""

    window_start, window_end = month_window(month)
    by_meter: dict[str, list[Sample]] = {}
    in_window = 0
    for sample in samples:
        if window_start <= sample.ts < window_end:
            in_window += 1
        by_meter.setdefault(sample.meter, []).append(sample)

    meter_results: list[MeterResult] = []
    for profile in sorted(profiles, key=lambda item: item.meter):
        meter_results.append(
            reconcile_meter(
                profile,
                by_meter.get(profile.meter, []),
                window_start,
                window_end,
                policy,
            )
        )

    raw_fingerprint = hashlib.sha256(
        repr(
            sorted(
                (sample.ts, sample.meter, sample.energy_kwh, sample.flow_t_h,
                 sample.supply_c, sample.return_c)
                for sample in samples
            )
        ).encode("utf-8")
    ).hexdigest()

    run = ReconciliationRun(
        run_id=run_id,
        month=month,
        window_start_ts=window_start,
        window_end_ts=window_end,
        created_ts=created_ts,
        policy=policy,
        meters=tuple(meter_results),
        raw_sample_count=in_window,
        raw_sha256=raw_fingerprint,
        partial_failure=(
            any(item.missing_segments for item in meter_results)
            or any(item.ambiguous_breaks for item in meter_results)
            or any(item.no_anchor_slots for item in meter_results)
            or any(item.bad_slots for item in meter_results)
        ),
    )
    _assert_run_identity(run)
    return run


def _assert_run_identity(run: ReconciliationRun) -> None:
    """批级恒等式与逐段汇总核对，保证补账、总账、审计取的是同一份数。"""

    totals = run.totals()
    sum_registered = sum(item.registered_kwh for item in run.meters)
    disclosed = 0.0
    fills_from_register = 0.0
    for meter in run.meters:
        for segment in meter.segments:
            if segment.is_filled():
                if segment.register_delta_kwh is not None:
                    fills_from_register += segment.register_delta_kwh
            elif segment.register_delta_kwh is not None:
                disclosed += segment.register_delta_kwh
    observed = totals["observed_kwh"]
    unbooked = sum_registered - observed - fills_from_register
    if abs(unbooked - disclosed) > 1e-6:
        raise ConsistencyError("批级总账不闭合：标缺段披露量与未入账读数差不一致")
    if abs(totals["unbooked_register_kwh"] - disclosed) > 1e-6:
        raise ConsistencyError("批级总账不闭合：汇总未入账读数差与逐段披露不一致")
    for meter in run.meters:
        for segment in meter.segments:
            if segment.is_filled() != (segment.reason in FILLED_REASONS):
                raise ConsistencyError("段状态与原因码不一致")
            if not segment.is_filled() and segment.reason not in MISSING_REASONS:
                raise ConsistencyError("标缺段原因码不在标缺码表内")
            if segment.is_filled() and segment.filled_kwh is None:
                raise ConsistencyError("已补段缺少入账热量")
            if not segment.is_filled() and segment.filled_kwh is not None:
                raise ConsistencyError("标缺段不得携带入账热量（不许硬凑）")
