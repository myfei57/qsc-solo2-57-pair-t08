"""断联补账：方法按表型、分档按阈值、缺条件标缺、账实审一致。"""

from __future__ import annotations

import pytest

from heatnet.config import QuotaLimits
from heatnet.console.api import Api
from heatnet.quota.backfill import (
    STATUS_FILLED,
    STATUS_MISSING,
    FlowTempSample,
    GapSegment,
    integrate_flow_temp,
    plan_gap,
    summarize_entries,
    unwrap_delta,
)
from heatnet.quota.meter import HeatMeter

LIMITS = QuotaLimits(10.0, 30.0, 24.0, 4.0, 21600.0)


def _samples(flow_t_h: float = 100.0, delta_t_c: float = 40.0) -> tuple[FlowTempSample, ...]:
    return tuple(
        FlowTempSample(ts=float(t), flow_t_h=flow_t_h, delta_t_c=delta_t_c)
        for t in range(0, 301, 30)
    )


def test_unwrap_delta_connects_readings_across_wrap():
    delta, wraps = unwrap_delta(8.0, 3.0, 10.0)
    assert delta == pytest.approx(5.0)
    assert wraps == 1
    delta, wraps = unwrap_delta(2.0, 7.0, 10.0)
    assert delta == pytest.approx(5.0)
    assert wraps == 0
    with pytest.raises(ValueError):
        unwrap_delta(-1.0, 3.0, 10.0)
    with pytest.raises(ValueError):
        unwrap_delta(1.0, 10.0, 10.0)


def test_integrate_flow_temp_requires_full_coverage():
    energy, covered = integrate_flow_temp(_samples(), 0.0, 300.0, 60.0)
    assert covered is True
    assert energy == pytest.approx(100.0 * 40.0 * 4.186 / 3600.0 * 300.0 / 3600.0)
    holed = tuple(sample for sample in _samples() if not 90.0 <= sample.ts <= 180.0)
    _energy, covered = integrate_flow_temp(holed, 0.0, 300.0, 60.0)
    assert covered is False
    _energy, covered = integrate_flow_temp((), 0.0, 300.0, 60.0)
    assert covered is False


def test_totalizing_fills_from_reading_delta_within_threshold():
    segment = GapSegment("m", 0.0, 300.0, reading_before_mwh=1.0, reading_after_mwh=4.0)
    plan = plan_gap(segment, "totalizing", LIMITS)
    assert plan.status == STATUS_FILLED
    assert plan.method == "interpolate"
    assert plan.backfill_mwh == pytest.approx(3.0)
    assert plan.difference["reading_gap_mwh"] == pytest.approx(3.0)
    assert "读数差" in plan.difference["note"]


def test_totalizing_falls_back_to_adjacent_rate_interpolation():
    segment = GapSegment("m", 0.0, 300.0, rate_before_mwh_s=0.001, rate_after_mwh_s=0.003)
    plan = plan_gap(segment, "totalizing", LIMITS)
    assert plan.status == STATUS_FILLED
    assert plan.backfill_mwh == pytest.approx(0.6)
    assert "插值" in plan.difference["note"]


def test_totalizing_without_readings_or_rates_is_missing():
    plan = plan_gap(GapSegment("m", 0.0, 300.0), "totalizing", LIMITS)
    assert plan.status == STATUS_MISSING
    assert "readings_missing" in plan.reasons
    assert plan.backfill_mwh == 0.0
    assert plan.difference["unresolved_mwh"] is None


def test_integrating_back_calculates_from_flow_and_delta_t():
    segment = GapSegment("m", 0.0, 300.0, samples=_samples())
    plan = plan_gap(segment, "integrating", LIMITS)
    assert plan.status == STATUS_FILLED
    assert plan.method == "flow_temp"
    assert plan.backfill_mwh == pytest.approx(0.3876, abs=1e-3)
    assert "反算" in plan.difference["note"]


def test_integrating_with_incomplete_samples_is_missing():
    holed = tuple(sample for sample in _samples() if not 90.0 <= sample.ts <= 180.0)
    plan = plan_gap(GapSegment("m", 0.0, 300.0, samples=holed), "integrating", LIMITS)
    assert plan.status == STATUS_MISSING
    assert "samples_incomplete" in plan.reasons
    assert plan.backfill_mwh == 0.0


def test_tiers_are_fixed_over_threshold_gap_or_wraps_means_missing():
    over = GapSegment("m", 0.0, 300.0, reading_before_mwh=8.0, reading_after_mwh=3.0)
    plan = plan_gap(over, "totalizing", LIMITS)
    assert plan.wraps == 1
    assert plan.status == STATUS_MISSING
    assert "over_full_threshold" in plan.reasons
    assert plan.difference["unresolved_mwh"] == pytest.approx(5.0)

    too_long = GapSegment("m", 0.0, 30000.0, reading_before_mwh=1.0, reading_after_mwh=2.0)
    plan = plan_gap(too_long, "totalizing", LIMITS)
    assert plan.status == STATUS_MISSING
    assert "gap_too_long" in plan.reasons


def test_collected_during_gap_is_deducted_not_duplicated():
    segment = GapSegment(
        "m", 0.0, 300.0, reading_before_mwh=1.0, reading_after_mwh=4.0, collected_mwh=1.0
    )
    plan = plan_gap(segment, "totalizing", LIMITS)
    assert plan.status == STATUS_FILLED
    assert plan.backfill_mwh == pytest.approx(2.0)
    assert plan.difference["collected_mwh"] == pytest.approx(1.0)


def test_plan_rejects_broken_segments():
    with pytest.raises(ValueError):
        plan_gap(GapSegment("m", 10.0, 5.0), "totalizing", LIMITS)
    with pytest.raises(ValueError):
        plan_gap(GapSegment("m", 0.0, 5.0, collected_mwh=-1.0), "totalizing", LIMITS)
    with pytest.raises(ValueError):
        plan_gap(GapSegment("m", 0.0, 5.0), "unknown-kind", LIMITS)
    with pytest.raises(ValueError):
        HeatMeter("bad", 10.0, kind="unknown-kind")


def test_summarize_entries_folds_journal():
    entries = [
        {"meter": "a", "method": "interpolate", "status": "filled", "backfill_mwh": 3.0},
        {
            "meter": "a",
            "method": "interpolate",
            "status": "missing",
            "backfill_mwh": 0.0,
            "difference": {"unresolved_mwh": 5.0},
        },
        {"meter": "b", "method": "flow_temp", "status": "filled", "backfill_mwh": 1.5},
    ]
    summary = summarize_entries(entries)
    assert summary["total"] == 3
    assert summary["filled"] == 2
    assert summary["missing"] == 1
    assert summary["backfilled_mwh"] == pytest.approx(4.5)
    assert summary["unresolved_mwh"] == pytest.approx(5.0)
    assert summary["by_meter"]["a"]["missing"] == 1


def test_platform_backfill_keeps_meter_ledger_journal_audit_consistent(platform):
    platform.collect_heat("st02.heat-meter", 2.0)
    report = platform.backfill_heat(
        "st02.heat-meter",
        [
            {
                "start_ts": 1000.0,
                "end_ts": 4600.0,
                "reading_before_mwh": 2.0,
                "reading_after_mwh": 7.0,
            }
        ],
    )
    assert report["ok"] is True
    entry = report["entries"][0]
    assert entry["method"] == "interpolate"
    assert entry["backfill_mwh"] == pytest.approx(5.0)
    meter = platform.meters.get("st02.heat-meter")
    assert meter.total_mwh() == pytest.approx(7.0)
    assert meter.reading_mwh() == pytest.approx(7.0)
    assert platform.ledger.allocated_mwh("st02") == pytest.approx(7.0)
    assert platform.ledger.backfilled_mwh("st02") == pytest.approx(5.0)
    assert platform.backfill_journal.count() == 1
    assert platform.audit.count("backfill") == 1
    line = [item for item in platform.billing(100.0) if item["station"] == "st02"][0]
    assert line["backfill_mwh"] == pytest.approx(5.0)
    assert line["collected_mwh"] == pytest.approx(2.0)
    quota = platform.quota_report()
    assert quota["backfill"]["entries"] == 1
    assert quota["ledger"]["backfill_mwh"] == pytest.approx(5.0)


def test_platform_backfill_integrating_uses_flow_temp(platform):
    report = platform.backfill_heat(
        "st01.heat-meter",
        [
            {
                "start_ts": 0.0,
                "end_ts": 300.0,
                "samples": [
                    {"ts": float(t), "flow_t_h": 120.0, "delta_t_c": 35.0}
                    for t in range(0, 301, 30)
                ],
            }
        ],
    )
    assert report["ok"] is True
    entry = report["entries"][0]
    assert entry["method"] == "flow_temp"
    expected = 120.0 * 35.0 * 4.186 / 3600.0 * 300.0 / 3600.0
    assert entry["backfill_mwh"] == pytest.approx(expected, abs=1e-4)
    assert platform.ledger.backfilled_mwh("st01") == pytest.approx(expected, abs=1e-4)


def test_platform_partial_failure_marks_missing_without_rolling_back(platform):
    report = platform.backfill_heat(
        "st02.heat-meter",
        [
            {"start_ts": 0.0, "end_ts": 600.0, "reading_before_mwh": 0.0, "reading_after_mwh": 2.0},
            {
                "start_ts": 1000.0,
                "end_ts": 4000.0,
                "reading_before_mwh": 2.0,
                "reading_after_mwh": 90000.0,
            },
        ],
    )
    assert report["ok"] is False
    assert report["missing"] == 1
    assert [entry["status"] for entry in report["entries"]] == ["filled", "missing"]
    missing = report["entries"][1]
    assert "over_full_threshold" in missing["reasons"]
    assert missing["backfill_mwh"] == 0.0
    assert missing["difference"]["unresolved_mwh"] == pytest.approx(89998.0)
    assert platform.ledger.backfilled_mwh("st02") == pytest.approx(2.0)
    assert platform.audit.count("backfill") == 1
    assert platform.audit.count("backfill_missing") == 1
    assert any(item["action"] == "backfill_missing" for item in platform.audit.alarms())


def test_platform_backfill_journal_is_append_only(platform):
    gap = {"start_ts": 0.0, "end_ts": 600.0, "reading_before_mwh": 0.0, "reading_after_mwh": 1.0}
    platform.backfill_heat("st02.heat-meter", [gap])
    before = platform.backfill_journal.read_all()
    platform.backfill_heat("st02.heat-meter", [dict(gap, start_ts=1000.0, end_ts=1600.0)])
    after = platform.backfill_journal.read_all()
    assert len(after) == len(before) + 1
    assert after[: len(before)] == before


def test_platform_settle_carries_backfill_split(platform):
    platform.collect_heat("st02.heat-meter", 2.0)
    platform.backfill_heat(
        "st02.heat-meter",
        [{"start_ts": 0.0, "end_ts": 600.0, "reading_before_mwh": 2.0, "reading_after_mwh": 5.0}],
    )
    settled = platform.settle_quota()
    assert settled["backfilled_mwh"]["st02"] == pytest.approx(3.0)
    assert platform.ledger.backfill_total_mwh() == 0.0
    assert platform.quota_report()["backfill"]["backfilled_mwh"] == pytest.approx(0.0)


def test_console_backfill_endpoints(platform):
    api = Api(platform)
    status, payload = api.handle(
        "POST",
        "/api/backfill",
        {},
        {
            "meter": "st04.heat-meter",
            "gap": {
                "start_ts": 0.0,
                "end_ts": 600.0,
                "reading_before_mwh": 0.0,
                "reading_after_mwh": 3.0,
            },
        },
    )
    assert status == 200
    assert payload["ok"] is True
    assert payload["backfilled_mwh"] == pytest.approx(3.0)
    status, payload = api.handle("GET", "/api/backfill", {}, {})
    assert status == 200
    assert payload["count"] == 1
    assert payload["summary"]["filled"] == 1
    assert payload["entries"][0]["station"] == "st04"
    status, payload = api.handle(
        "POST", "/api/backfill", {}, {"meter": "nope", "gap": {"start_ts": 0, "end_ts": 1}}
    )
    assert status == 404
    status, payload = api.handle("POST", "/api/backfill", {}, {"meter": "st04.heat-meter"})
    assert status == 400
    status, payload = api.handle(
        "POST",
        "/api/backfill",
        {},
        {"meter": "st04.heat-meter", "gap": {"start_ts": 10.0, "end_ts": 1.0}},
    )
    assert status == 400


def test_backfill_journal_survives_restart(platform, clock):
    from heatnet.app import build_platform
    from heatnet.config import default_config

    platform.backfill_heat(
        "st02.heat-meter",
        [{"start_ts": 0.0, "end_ts": 600.0, "reading_before_mwh": 0.0, "reading_after_mwh": 2.0}],
    )
    restarted = build_platform(default_config(), clock=clock, storage_root=platform.store.root)
    report = restarted.backfill_report()
    assert report["count"] == 1
    assert report["summary"]["filled"] == 1
    assert report["entries"][0]["backfill_mwh"] == pytest.approx(2.0)
