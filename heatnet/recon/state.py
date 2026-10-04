"""断联补账的不可变数据结构。

口径定死在配置里（见 :data:`DEFAULT_POLICY`），运行期只能照着执行，不能按单次
补账临时改方法或改阈值：

* 补算方法按表型定：``rate``（有流量、供回温分量）按 流量×温差 反算瞬时功率做
  梯形积分；``total``（只有累计热量寄存器）优先用回绕解绕后的两端读数差分配，
  仅短缺口且寄存器读数不可用时，才退回相邻时段增量插值。
* 缺口按阈值分档定死，不做"回绕前一律补满"：

  - ``short``  缺口 ≤ ``short_slots`` 个采集槽，允许估算（分量梯形 / 插值）；
  - ``mid``    超过短档但 ≤ ``max_gap_hours`` 小时，只接受有物理依据的补法，
               total 表不允许跨缺口邻居插值；
  - ``long``   超过 ``max_gap_hours`` 一律标缺，即使 total 表两端读数差能量化，
               也只在差异说明里挂账披露（unbooked_register），不计入入账热量。

* 回绕候选有 0 个（坏点）或多于 1 个（无法唯一解绕）都不硬凑：多解断链、标缺。

原始采样只读；补账结果只追加。所有字段口径在引擎、总账、审计、控制台之间一致。
"""

from __future__ import annotations

from dataclasses import dataclass, field

# ---- 固定口径（策略常量，不是每次补账的可调参数） ---------------------------

# 短档上限：缺口不超过这么多个采集槽，才允许估算/插值。
DEFAULT_SHORT_SLOTS = 2
# 长档门槛：缺口超过这么多小时，一律标缺（即便两端寄存器读数差能量化）。
DEFAULT_MAX_GAP_HOURS = 24.0
# 反算功率与寄存器读数差对账的相对容差：|残差|/读数差 超过它即判对账失败、标缺。
DEFAULT_CLOSURE_TOLERANCE = 0.02
# total 表"读数冻住"判定：两端读数差为 0，但两端平均瞬时功率折算的槽增量高于该值
# （kWh/槽）时认为寄存器停走；只有短档才允许按相邻时段插值补。
DEFAULT_FROZEN_KWH_PER_SLOT = 0.05


# ---- 原因码：每一个补账段、每一个坏槽都必须带一个，审计和展示共用 --------------

# 可补（入账为估算量，带 evidence）
RATE_TRAPEZOID = "RATE_TRAPEZOID"          # rate 表：流量温差反算梯形积分
TOTAL_REGISTER_ALLOC = "TOTAL_REGISTER_ALLOC"  # total 表：寄存器读数差线性分配
TOTAL_NEIGHBOR_INTERP = "TOTAL_NEIGHBOR_INTERP"  # total 表短档：相邻时段增量插值
# 标缺（部分失败，缺条件的一段宁可标缺，不许硬凑；不给入账热量值）
MISSING_LONG = "MISSING_LONG"              # 缺口超过长档门槛
MISSING_CONDITION = "MISSING_CONDITION"    # 缺流量/温差等反算条件
MISSING_NEIGHBOR = "MISSING_NEIGHBOR"      # total 短档插值缺合格邻居
RATE_CLOSURE_MISMATCH = "RATE_CLOSURE_MISMATCH"  # 反算与寄存器差超容差
AMBIGUOUS_ROLLOVER = "AMBIGUOUS_ROLLOVER"  # 回绕次数无法唯一确定
BAD_SAMPLE = "BAD_SAMPLE"                  # 读数跳变越限（单槽坏点）
NO_ANCHOR = "NO_ANCHOR"                    # 缺口一侧没有可用读数锚点
FILLED_REASONS = frozenset((RATE_TRAPEZOID, TOTAL_REGISTER_ALLOC, TOTAL_NEIGHBOR_INTERP))
MISSING_REASONS = frozenset(
    (
        MISSING_LONG,
        MISSING_CONDITION,
        MISSING_NEIGHBOR,
        RATE_CLOSURE_MISMATCH,
        AMBIGUOUS_ROLLOVER,
        NO_ANCHOR,
    )
)

TIER_SHORT = "short"
TIER_MID = "mid"
TIER_LONG = "long"
TIERS = (TIER_SHORT, TIER_MID, TIER_LONG)


@dataclass(frozen=True)
class Sample:
    """一条原始热量表采样。原始数据只读，任何处理都不改它。

    ``energy_kwh`` 是表头累计热量读数（kWh，可能已回绕）；``flow_t_h``、
    ``supply_c``、``return_c`` 是分量表的瞬时流量与供回温。分量缺失用 ``None``。
    """

    ts: float
    meter: str
    energy_kwh: float | None
    flow_t_h: float | None = None
    supply_c: float | None = None
    return_c: float | None = None

    def as_dict(self) -> dict:
        return {
            "ts": self.ts,
            "meter": self.meter,
            "energy_kwh": self.energy_kwh,
            "flow_t_h": self.flow_t_h,
            "supply_c": self.supply_c,
            "return_c": self.return_c,
        }


@dataclass(frozen=True)
class MeterProfile:
    """一块表的表型与物理参数。

    ``kind="rate"`` 表示该表上报流量/温差分量，断联段按分量反算；
    ``kind="total"`` 表示只有累计热量寄存器，断联段按读数差/邻居插值。
    ``k_factor`` 是功率换算系数：``P_kW = k_factor * 流量 * (供温-回温)``，
    流量取 t/h 时取 1.163。
    """

    meter: str
    kind: str
    wrap_kwh: float
    period_s: float
    k_factor: float = 1.163
    max_power_kw: float | None = None

    def as_dict(self) -> dict:
        return {
            "meter": self.meter,
            "kind": self.kind,
            "wrap_kwh": self.wrap_kwh,
            "period_s": self.period_s,
            "k_factor": self.k_factor,
            "max_power_kw": self.max_power_kw,
        }


@dataclass(frozen=True)
class Policy:
    """分档与判定口径。默认值即定死口径，配置只能原样转述。"""

    short_slots: int = DEFAULT_SHORT_SLOTS
    max_gap_hours: float = DEFAULT_MAX_GAP_HOURS
    closure_tolerance: float = DEFAULT_CLOSURE_TOLERANCE
    frozen_kwh_per_slot: float = DEFAULT_FROZEN_KWH_PER_SLOT

    def as_dict(self) -> dict:
        return {
            "short_slots": self.short_slots,
            "max_gap_hours": self.max_gap_hours,
            "closure_tolerance": self.closure_tolerance,
            "frozen_kwh_per_slot": self.frozen_kwh_per_slot,
            # 定死声明：不按"回绕前一律补满"，按阈值分档处理。
            "fill_rule": "threshold_tiered",
        }


DEFAULT_POLICY = Policy()


@dataclass(frozen=True)
class Segment:
    """两个可用读数锚点之间的一段补账结论（或一个标缺段）。

    估算段（``reason`` 属于 :data:`FILLED_REASONS`）：``filled_kwh`` 有值，是入账
    的估算热量；标缺段：``filled_kwh`` 为 ``None``，段不进入账总量，只在差异说明
    里披露时长与原因。

    ``anchor_before`` / ``anchor_after`` 给出回绕解绕后的连续读数（kWh），让回绕
    前后的读数能接上；原始表头读数仍保留在 ``raw_*`` 字段里，不改历史。
    """

    meter: str
    start_ts: float
    end_ts: float
    missing_slots: int
    tier: str
    reason: str
    filled_kwh: float | None
    method: str | None
    register_delta_kwh: float | None = None
    residual_kwh: float | None = None
    anchor_before_kwh: float | None = None
    anchor_after_kwh: float | None = None
    raw_before_kwh: float | None = None
    raw_after_kwh: float | None = None
    wraps: int = 0
    # rate 表反算用的边界条件；total 表冻住判定用得上。
    power_before_kw: float | None = None
    power_after_kw: float | None = None
    slot_kwh: tuple[float | None, ...] = field(default_factory=tuple)

    def is_filled(self) -> bool:
        return self.reason in FILLED_REASONS

    def as_dict(self) -> dict:
        return {
            "meter": self.meter,
            "start_ts": self.start_ts,
            "end_ts": self.end_ts,
            "missing_slots": self.missing_slots,
            "tier": self.tier,
            "reason": self.reason,
            "status": "FILLED" if self.is_filled() else "MISSING",
            "filled_kwh": self.filled_kwh,
            "method": self.method,
            "register_delta_kwh": self.register_delta_kwh,
            "residual_kwh": self.residual_kwh,
            "anchor_before_kwh": self.anchor_before_kwh,
            "anchor_after_kwh": self.anchor_after_kwh,
            "raw_before_kwh": self.raw_before_kwh,
            "raw_after_kwh": self.raw_after_kwh,
            "wraps": self.wraps,
            "power_before_kw": self.power_before_kw,
            "power_after_kw": self.power_after_kw,
            "slot_kwh": list(self.slot_kwh),
        }


@dataclass(frozen=True)
class BadSlot:
    """一个被隔离的槽位（缺采样或坏点）。坏点两侧仍有读数锚点时，由段结论处理。"""

    meter: str
    ts: float
    reason: str
    raw_kwh: float | None = None

    def as_dict(self) -> dict:
        return {
            "meter": self.meter,
            "ts": self.ts,
            "reason": self.reason,
            "raw_kwh": self.raw_kwh,
        }


@dataclass(frozen=True)
class MeterResult:
    """一块表一个月的补账结论。"""

    meter: str
    kind: str
    wrap_kwh: float
    period_s: float
    segments: tuple[Segment, ...]
    bad_slots: tuple[BadSlot, ...]
    # 可解绕链上的原始/连续读数（连续口径，回绕前后接得上）。
    anchors: tuple[tuple[float, float, float], ...]  # (ts, raw_kwh, unwrapped_kwh)
    # 总账口径（kWh）：
    observed_kwh: float    # 连续好点之间直接读到的增量
    filled_kwh: float      # 已补、入账的估算增量（读数差分配/反算/插值）
    registered_kwh: float  # 连续好点之间的寄存器读数差合计
    missing_gap_kwh: float  # 标缺段挂账披露量（未入账读数差），非入账
    residual_kwh: float    # rate 已补段：反算原始量与读数差的残差合计（披露用）
    correction_kwh: float  # total 冻住插值的表计修正量（读数差为 0、靠旁证补入）
    missing_segments: int
    filled_segments: int
    ambiguous_breaks: int
    no_anchor_slots: int
    max_power_kw: float | None = None

    @property
    def booked_kwh(self) -> float:
        """入账热量 = 实读增量 + 已补估算增量（标缺段不硬凑进来）。"""

        return self.observed_kwh + self.filled_kwh

    def as_dict(self) -> dict:
        return {
            "meter": self.meter,
            "kind": self.kind,
            "wrap_kwh": self.wrap_kwh,
            "period_s": self.period_s,
            "segments": [segment.as_dict() for segment in self.segments],
            "bad_slots": [slot.as_dict() for slot in self.bad_slots],
            "anchors": [
                {"ts": ts, "raw_kwh": raw, "unwrapped_kwh": unwrapped}
                for ts, raw, unwrapped in self.anchors
            ],
            "observed_kwh": self.observed_kwh,
            "filled_kwh": self.filled_kwh,
            "registered_kwh": self.registered_kwh,
            "missing_gap_kwh": self.missing_gap_kwh,
            "residual_kwh": self.residual_kwh,
            "correction_kwh": self.correction_kwh,
            "booked_kwh": self.booked_kwh,
            "filled_segments": self.filled_segments,
            "missing_segments": self.missing_segments,
            "ambiguous_breaks": self.ambiguous_breaks,
            "no_anchor_slots": self.no_anchor_slots,
            "max_power_kw": self.max_power_kw,
        }


@dataclass(frozen=True)
class ReconciliationRun:
    """一次补账批处理的完整结果。补账结果只追加，历史不可变。"""

    run_id: str
    month: str
    window_start_ts: float
    window_end_ts: float
    created_ts: float
    policy: Policy
    meters: tuple[MeterResult, ...]
    raw_sample_count: int
    raw_sha256: str
    partial_failure: bool

    def totals(self) -> dict:
        """总账：补账、总账、审计共用同一份汇总，任何展示都从这里取数。"""

        observed = sum(item.observed_kwh for item in self.meters)
        filled = sum(item.filled_kwh for item in self.meters)
        registered = sum(item.registered_kwh for item in self.meters)
        missing_gap = sum(item.missing_gap_kwh for item in self.meters)
        residual = sum(item.residual_kwh for item in self.meters)
        correction = sum(item.correction_kwh for item in self.meters)
        return {
            "meters": len(self.meters),
            "observed_kwh": observed,
            "filled_kwh": filled,
            "booked_kwh": observed + filled,
            "registered_kwh": registered,
            "unbooked_register_kwh": registered - (observed + filled - correction),
            "missing_gap_kwh": missing_gap,
            "residual_kwh": residual,
            "meter_correction_kwh": correction,
            "filled_segments": sum(item.filled_segments for item in self.meters),
            "missing_segments": sum(item.missing_segments for item in self.meters),
            "ambiguous_breaks": sum(item.ambiguous_breaks for item in self.meters),
            "no_anchor_slots": sum(item.no_anchor_slots for item in self.meters),
            "bad_slots": sum(len(item.bad_slots) for item in self.meters),
            "partial_failure": self.partial_failure,
        }

    def as_dict(self) -> dict:
        return {
            "run_id": self.run_id,
            "month": self.month,
            "window_start_ts": self.window_start_ts,
            "window_end_ts": self.window_end_ts,
            "created_ts": self.created_ts,
            "policy": self.policy.as_dict(),
            "raw_sample_count": self.raw_sample_count,
            "raw_sha256": self.raw_sha256,
            "partial_failure": self.partial_failure,
            "meters": [item.as_dict() for item in self.meters],
            "totals": self.totals(),
        }
