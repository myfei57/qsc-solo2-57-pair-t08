"""断联补账（reconciliation）。

按表型决定补法、按阈值分档决定能不能补；原始数据不可变，补账只追加；缺条件
宁可标缺不许硬凑；补账、总账、审计同源一致。口径见 :mod:`heatnet.recon.state`。
"""

from __future__ import annotations

from .engine import (
    ConsistencyError,
    ReconcileConfigError,
    month_window,
    rate_power_kw,
    reconcile,
    reconcile_meter,
)
from .report import difference_report, meter_difference_lines, render_console, run_payload, to_json
from .service import (
    ReconcileService,
    demo_csv,
    demo_dataset,
    load_csv,
    load_profiles,
)
from .state import (
    DEFAULT_POLICY,
    FILLED_REASONS,
    MISSING_REASONS,
    BadSlot,
    MeterProfile,
    MeterResult,
    Policy,
    ReconciliationRun,
    Sample,
    Segment,
)
from .store import ReconcileStore

__all__ = [
    "BadSlot",
    "ConsistencyError",
    "DEFAULT_POLICY",
    "FILLED_REASONS",
    "MISSING_REASONS",
    "MeterProfile",
    "MeterResult",
    "Policy",
    "ReconcileConfigError",
    "ReconciliationRun",
    "ReconcileService",
    "ReconcileStore",
    "Sample",
    "Segment",
    "demo_csv",
    "demo_dataset",
    "difference_report",
    "load_csv",
    "load_profiles",
    "meter_difference_lines",
    "month_window",
    "rate_power_kw",
    "reconcile",
    "reconcile_meter",
    "render_console",
    "run_payload",
    "to_json",
]
