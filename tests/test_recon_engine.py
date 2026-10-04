"""断联补账：表型分流、阈值分档、回绕接读、标缺不硬凑、总账闭合。

原始数据构造以"槽"为单位（每小时一槽），不依赖真实日历长度。
"""

from __future__ import annotations

import math

import pytest

from heatnet.recon.engine import (
    ConsistencyError,
    ReconcileConfigError,
    month_window,
    reconcile,
    reconcile_meter,
)
from heatnet.recon.state import (
    AMBIGUOUS_ROLLOVER,
    BAD_SAMPLE,
    DEFAULT_POLICY,
    MISSING_CONDITION,
    MISSING_LONG,
    MISSING_NEIGHBOR,
    NO_ANCHOR,
    MeterProfile,
    Policy,
    RATE_CLOSURE_MISMATCH,
    RATE_TRAPEZOID,
    Sample,
    TOTAL_NEIGHBOR_INTERP,
    TOTAL_REGISTER_ALLOC,
)

MONTH = "2026-09"
START, END = month_window(MONTH)
HOUR = 3600.0


def ts(slot: int) -> float:
    return START + slot * HOUR


def rate_sample(slot: int, energy: float, power: float = 10.0, flow: float = 30.0) -> Sample:
    dt_c = power / 1.163 / flow
    return Sample(ts(slot), "R", energy, flow, 80.0, 80.0 - dt_c)


def total_sample(slot: int, raw: float, **components) -> Sample:
    return Sample(ts(slot), "T", raw, components.get("flow"), components.get("supply"),
                  components.get("ret"))


def run_rate(samples, **profile_kw) -> object:
    profile = MeterProfile("R", "rate", wrap_kwh=profile_kw.pop("wrap_kwh", 1_000_000.0),
                           period_s=HOUR, max_power_kw=profile_kw.pop("max_power_kw", 100.0))
    return reconcile_meter(profile, samples, START, END, Policy())


def run_total(samples, *, wrap_kwh=1000.0, max_power_kw=None, policy=Policy()) -> object:
    profile = MeterProfile("T", "total", wrap_kwh=wrap_kwh, period_s=HOUR,
                           max_power_kw=max_power_kw)
    return reconcile_meter(profile, samples, START, END, policy)


# ---- 回绕解绕与接上 ----------------------------------------------------------


def test_total_meter_unwraps_rollover_and_keeps_anchors_continuous():
    # wrap=1000：槽0读数 990，槽1/2断联，槽3读数 20，唯一解绕为 1020（差 30）。
    samples = [total_sample(0, 990.0), total_sample(3, 20.0), total_sample(4, 50.0)]
    result = run_total(samples, wrap_kwh=1000.0, max_power_kw=100.0)
    assert result.filled_segments == 1
    segment = result.segments[0]
    assert segment.reason == TOTAL_REGISTER_ALLOC
    assert segment.wraps == 1
    assert segment.filled_kwh == pytest.approx(30.0)
    # 回绕前后读数接得上：连续口径 990 → 1020。
    assert segment.anchor_before_kwh == pytest.approx(990.0)
    assert segment.anchor_after_kwh == pytest.approx(1020.0)
    assert segment.raw_before_kwh == pytest.approx(990.0)
    assert segment.raw_after_kwh == pytest.approx(20.0)
    assert tuple(result.anchors) == (
        (ts(0), 990.0, 990.0),
        (ts(3), 20.0, 1020.0),
        (ts(4), 50.0, 1050.0),
    )
    # 逐槽线性分配：缺口 2 个采样槽，读数差 30 → 每槽 15，不丢不重。
    assert list(segment.slot_kwh) == [15.0, 15.0]


def test_rate_meter_trapezoid_fill_matches_register_and_closes():
    # 恒功率 10 kW：槽0→槽4 跨度 4 小时，梯形积分恰好 40 kWh，与读数差闭合。
    samples = [rate_sample(0, 0.0, power=10.0)]
    samples += [rate_sample(4, 40.0, power=10.0)]
    result = run_rate(samples, max_power_kw=100.0)
    segment = result.segments[0]
    assert segment.reason == RATE_TRAPEZOID
    assert segment.filled_kwh == pytest.approx(40.0)
    assert segment.residual_kwh == pytest.approx(0.0, abs=1e-9)
    assert result.registered_kwh == pytest.approx(40.0)
    assert result.booked_kwh == pytest.approx(40.0)


# ---- 按表型分流：total 表不许反算，rate 表不许读数差硬分 ------------------------


def test_rate_meter_missing_components_is_marked_missing_not_register_alloc():
    # rate 表缺口恢复帧没有流量/温差：即使读数差已知也标缺（缺条件不硬凑）。
    samples = [rate_sample(0, 0.0, power=10.0)]
    samples.append(Sample(ts(2), "R", 15.0))  # 无分量
    result = run_rate(samples)
    segment = result.segments[0]
    assert segment.is_filled() is False
    assert segment.reason == MISSING_CONDITION
    assert segment.filled_kwh is None
    # 读数差 15 kWh 披露但不入账。
    assert segment.register_delta_kwh == pytest.approx(15.0)
    assert result.missing_gap_kwh == pytest.approx(15.0)
    assert result.booked_kwh == pytest.approx(0.0)


def test_rate_meter_closure_mismatch_is_marked_missing():
    # 槽0→槽3 跨度 3 小时，分量说产生 ~30 kWh，寄存器却跳了 40，超 2% 容差
    # → 整段标缺，残差 10 kWh 只披露不入账。
    samples = [rate_sample(0, 0.0, power=10.0)]
    samples += [rate_sample(3, 40.0, power=10.0)]
    result = run_rate(samples, max_power_kw=100.0)
    segment = result.segments[0]
    assert segment.reason == RATE_CLOSURE_MISMATCH
    assert segment.filled_kwh is None
    assert segment.residual_kwh == pytest.approx(10.0, abs=1e-9)


# ---- 阈值分档：长档一律标缺（不按回绕前一律补满） -----------------------------


def test_long_gap_is_never_filled_even_when_register_delta_known():
    # 25 槽（>24h）长缺口，total 表读数差明确给 250 kWh，仍必须标缺。
    # 功率上限 10 kW×26 槽只容许 260 kWh，保证 0 次回绕是唯一解（先解绕，
    # 再由长档规则拒绝补入），而不是被多解逻辑拦截。
    samples = [total_sample(0, 0.0), total_sample(26, 250.0)]
    result = run_total(samples, wrap_kwh=1000.0, max_power_kw=10.0)
    segment = result.segments[0]
    assert segment.tier == "long"
    assert segment.reason == MISSING_LONG
    assert segment.filled_kwh is None
    assert result.filled_kwh == 0.0
    assert result.missing_gap_kwh == pytest.approx(250.0)
    assert result.booked_kwh == pytest.approx(0.0)
    assert result.missing_segments == 1


def test_tier_boundaries_short_mid_long():
    # 短档 ≤2 槽；中档 >2 槽且段时长（missing+1 小时）≤24h；否则长档。
    def make(missing: int):
        return [total_sample(0, 0.0), total_sample(missing + 1, 10.0 * (missing + 1))]

    assert run_total(make(2)).segments[0].tier == "short"
    assert run_total(make(3)).segments[0].tier == "mid"
    assert run_total(make(23)).segments[0].tier == "mid"   # 24 小时，恰好中上限
    assert run_total(make(24)).segments[0].tier == "long"  # 25 小时
    assert run_total(make(25)).segments[0].tier == "long"
    # 短档/中档有读数差都补；长档不补。
    assert run_total(make(2)).segments[0].is_filled()
    assert run_total(make(3)).segments[0].is_filled()
    assert run_total(make(25)).segments[0].is_filled() is False


def test_policy_fill_rule_is_threshold_tiered_not_fill_to_wraparound():
    assert DEFAULT_POLICY.as_dict()["fill_rule"] == "threshold_tiered"


# ---- total 表冻住插值：仅短档 + 合格邻居 + 旁证在用热 ---------------------------


def test_total_frozen_register_short_gap_uses_neighbor_interpolation():
    # 每槽 30 kWh。槽0/1 正常（0、30），槽2 断联，槽3 读数冻在 30（与槽1相同），
    # 但流量温差显示约 35 kW 在用热；槽4 恢复递增到 60。两侧邻居各 30 kWh/槽。
    samples = [
        total_sample(0, 0.0),
        total_sample(1, 30.0),
        total_sample(3, 30.0, flow=2.0, supply=75.0, ret=59.93),
        total_sample(4, 60.0),
    ]
    result = run_total(samples, wrap_kwh=1000.0, max_power_kw=60.0)
    assert result.filled_segments == 1
    segment = result.segments[0]
    assert segment.reason == TOTAL_NEIGHBOR_INTERP
    assert segment.register_delta_kwh == pytest.approx(0.0)
    assert segment.filled_kwh == pytest.approx(30.0)
    assert result.filled_kwh == pytest.approx(30.0)
    assert result.correction_kwh == pytest.approx(30.0)  # 读数差为0的表计修正
    # 入账 = 实读 60（槽1→3 读数差0 + 槽3→4 30…）等见下，逐表恒等式单独核对。
    assert result.booked_kwh == pytest.approx(90.0)


def test_total_frozen_mid_gap_is_not_interpolated():
    # 读数冻住（恢复帧读数与断联前相同，读数差 0）但缺口 3 槽（中档）：
    # 禁止邻居插值，标缺。
    samples = [
        total_sample(0, 0.0),
        total_sample(4, 0.0, flow=2.0, supply=75.0, ret=59.93),
        total_sample(5, 30.0),
    ]
    result = run_total(samples, wrap_kwh=1000.0, max_power_kw=60.0)
    segment = result.segments[0]
    assert segment.is_filled() is False
    assert segment.reason == MISSING_CONDITION


def test_total_short_gap_without_neighbors_is_marked_missing():
    # 槽0 首读数（其左没有邻居），槽1 断联，槽2 恢复帧读数冻在槽0 的值且旁证
    # 显示在用热：右邻居可用但左邻居缺失 → 不插值，标缺。
    samples = [
        total_sample(0, 0.0),
        total_sample(2, 0.0, flow=2.0, supply=75.0, ret=59.93),
        total_sample(3, 30.0),
    ]
    result = run_total(samples, wrap_kwh=1000.0, max_power_kw=60.0)
    segment = result.segments[0]
    assert segment.reason == MISSING_NEIGHBOR
    assert segment.filled_kwh is None


# ---- 回绕多解：断链标缺，不猜一个 --------------------------------------------


def test_ambiguous_rollover_breaks_chain_and_marks_missing():
    # wrap=200，每槽 20 kWh；槽0~3 正常（…60），槽4~33 断联（30 槽），
    # 槽34 raw=80；功率上限 25 kW × 34 槽允许 850 kWh：
    # 候选 80/280/480/680/880(超)，多个合法 → 回绕次数多解。
    samples = [total_sample(slot, (slot * 20.0) % 200.0) for slot in range(4)]
    samples.append(total_sample(34, 80.0))
    result = run_total(samples, wrap_kwh=200.0, max_power_kw=25.0)
    assert result.ambiguous_breaks == 1
    segment = result.segments[0]
    assert segment.reason == AMBIGUOUS_ROLLOVER
    assert segment.filled_kwh is None
    assert segment.register_delta_kwh is None  # 多解时读数差不可量化
    assert any(slot.reason == AMBIGUOUS_ROLLOVER for slot in result.bad_slots)
    # 多解点之后链可以重新起步，不影响之后的正常读数。
    samples.append(total_sample(35, 100.0))
    result2 = run_total(samples, wrap_kwh=200.0, max_power_kw=25.0)
    assert result2.ambiguous_breaks == 1


def test_bad_spike_is_isolated_but_chain_resumes_after():
    # 槽1 读数凭空大 5000：隔离坏点；槽0→槽2 之间形成缺口段（rate 表可反算补）。
    samples = [
        rate_sample(0, 0.0, power=10.0),
        rate_sample(1, 5000.0, power=10.0),
        rate_sample(2, 20.0, power=10.0),
    ]
    result = run_rate(samples, max_power_kw=100.0)
    spikes = [slot for slot in result.bad_slots if slot.reason == BAD_SAMPLE]
    assert [slot.reason for slot in spikes] == [BAD_SAMPLE]
    assert spikes[0].raw_kwh == pytest.approx(5000.0)
    # 槽0→槽2 段缺 1 槽（跨度 2 小时），反算闭合 → 已补。
    assert result.filled_segments == 1
    assert result.segments[0].filled_kwh == pytest.approx(20.0)


# ---- 月窗边缘无锚点：标缺 NO_ANCHOR，不补 -------------------------------------


def test_leading_and_trailing_slots_without_anchor_are_missing():
    samples = [total_sample(5, 50.0), total_sample(6, 60.0)]
    result = run_total(samples, wrap_kwh=1000.0, max_power_kw=100.0)
    n_slots = int((END - START) / HOUR)
    # 前 5 槽 + 月末所有无采样槽都缺锚点。
    assert result.no_anchor_slots == 5 + (n_slots - 7)
    assert {slot.reason for slot in result.bad_slots} == {NO_ANCHOR}
    assert result.booked_kwh == pytest.approx(10.0)  # 只结算锚点之间


# ---- 总账闭合与一致性 --------------------------------------------------------


def test_registered_identity_observed_plus_filled_plus_disclosed():
    # total 表：一段中档补入 + 一段长档标缺披露。
    samples = [
        total_sample(0, 0.0),
        total_sample(5, 100.0),    # 4 槽中档，补 100
        total_sample(31, 400.0),   # 25 槽长档，读数差 300，标缺
    ]
    result = run_total(samples, wrap_kwh=1000.0, max_power_kw=20.0)
    assert result.registered_kwh == pytest.approx(400.0)
    assert result.observed_kwh + result.filled_kwh + result.missing_gap_kwh == pytest.approx(400.0)
    assert result.filled_kwh == pytest.approx(100.0)
    assert result.missing_gap_kwh == pytest.approx(300.0)
    assert result.booked_kwh == pytest.approx(100.0)


def test_batch_totals_equal_sum_of_meters_and_partial_failure_flag():
    profiles = [
        MeterProfile("A", "total", wrap_kwh=1000.0, period_s=HOUR, max_power_kw=100.0),
        MeterProfile("B", "total", wrap_kwh=1000.0, period_s=HOUR, max_power_kw=10.0),
    ]
    samples = [
        Sample(ts(0), "A", 0.0), Sample(ts(4), "A", 80.0),   # 中档补入
        Sample(ts(0), "B", 0.0), Sample(ts(30), "B", 300.0),  # 长档标缺
    ]
    run = reconcile(MONTH, profiles, samples, created_ts=START, run_id="r1")
    totals = run.totals()
    summed = sum(meter.booked_kwh for meter in run.meters)
    assert totals["booked_kwh"] == pytest.approx(summed)
    assert totals["booked_kwh"] == pytest.approx(80.0)
    assert totals["missing_segments"] == 1
    assert run.partial_failure is True
    # 差异说明与总账同源。
    payload = run.as_dict()
    assert payload["totals"] == totals


def test_inconsistent_setup_raises_consistency_error_is_never_swallowed():
    # 周期不能整除月窗：配置错误直接抛出，不能静默写半份账。
    profile = MeterProfile("T", "total", wrap_kwh=1000.0, period_s=3700.0)
    with pytest.raises(ReconcileConfigError):
        reconcile_meter(profile, [], START, END, Policy())


def test_unknown_meter_kind_rejected():
    profile = MeterProfile("T", "weird", wrap_kwh=1000.0, period_s=HOUR)
    with pytest.raises(ReconcileConfigError):
        run_total_force(profile)


def run_total_force(profile) -> None:
    reconcile_meter(profile, [], START, END, Policy())


# ---- 原始数据只读 ------------------------------------------------------------


def test_reconcile_does_not_mutate_samples():
    samples = [total_sample(0, 990.0), total_sample(3, 20.0)]
    before = [(s.ts, s.energy_kwh) for s in samples]
    run_total(samples, wrap_kwh=1000.0, max_power_kw=100.0)
    assert [(s.ts, s.energy_kwh) for s in samples] == before
