"""供热配额与热量表。

热量表按周期累计热量，读数到顶后回绕，总账要同时给出"表读数"和"实际累计"两个
口径。多个采集任务会并发累加同一块表，累加必须是原子的：非原子的 ``+=`` 在并发
下会丢增量，总账比实际小。断联缺口按表型补账：积算式按流量温差反算，总量式按
前后读数差或相邻时段插值；补账只追加不改写，表、总账、流水与审计保持一致。
"""

from __future__ import annotations

from .accumulator import QuotaAccumulator
from .backfill import (
    METHOD_FLOW_TEMP,
    METHOD_INTERPOLATE,
    STATUS_FILLED,
    STATUS_MISSING,
    BackfillPlan,
    FlowTempSample,
    GapSegment,
    integrate_flow_temp,
    method_for_kind,
    plan_gap,
    summarize_entries,
    unwrap_delta,
)
from .billing import billing_lines
from .meter import INTEGRATING, METER_KINDS, TOTALIZING, HeatMeter
from .quota import QuotaLedger
from .state import MeterSnapshot, QuotaSnapshot

__all__ = [
    "BackfillPlan",
    "FlowTempSample",
    "GapSegment",
    "HeatMeter",
    "INTEGRATING",
    "METER_KINDS",
    "METHOD_FLOW_TEMP",
    "METHOD_INTERPOLATE",
    "MeterSnapshot",
    "QuotaAccumulator",
    "QuotaLedger",
    "QuotaSnapshot",
    "STATUS_FILLED",
    "STATUS_MISSING",
    "TOTALIZING",
    "billing_lines",
    "integrate_flow_temp",
    "method_for_kind",
    "plan_gap",
    "summarize_entries",
    "unwrap_delta",
]
