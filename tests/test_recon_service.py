"""补账服务：执行落账、审计留痕、差异说明与控制台 JSON 同源、重跑只追加。"""

from __future__ import annotations

import io

import pytest

from heatnet.recon.report import difference_report, render_console, run_payload
from heatnet.recon.service import ReconcileService, demo_dataset, load_csv, load_profiles
from heatnet.recon.state import (
    MISSING_CONDITION,
    MISSING_LONG,
    RATE_TRAPEZOID,
    TOTAL_NEIGHBOR_INTERP,
    TOTAL_REGISTER_ALLOC,
)
from heatnet.recon.store import ReconcileStore


def test_service_run_appends_ledger_and_audit_and_is_partial(tmp_path):
    store = ReconcileStore(str(tmp_path))
    service = ReconcileService(store, now=1_760_000_000.0)
    profiles, samples = demo_dataset("2026-09")

    run = service.run("2026-09", profiles, samples, source="unit-test")
    assert run.partial_failure is True

    records = store.list_runs("2026-09")
    assert len(records) == 1
    assert records[0]["run_no"] == 0

    entries = store.audit_entries()
    actions = [entry["action"] for entry in entries]
    assert "raw.imported" in actions
    assert "recon.run" in actions
    # 每一个标缺段都有独立 warning 审计。
    missing_audits = [entry for entry in entries if entry["action"] == "recon.segment_missing"]
    assert len(missing_audits) == run.totals()["missing_segments"]
    assert all(entry["severity"] == "warning" for entry in missing_audits)
    # 口径定死在每条批次的 recon.policy 审计里：阈值分档，不是回绕前一律补满。
    policy_audit = next(entry for entry in entries if entry["action"] == "recon.policy")
    assert policy_audit["payload"]["fill_rule"] == "threshold_tiered"
    run_audit = next(entry for entry in entries if entry["action"] == "recon.run")
    assert run_audit["severity"] == "warning"
    assert store.verify_chain()["ok"] is True


def test_rerun_does_not_mutate_history_and_latest_wins(tmp_path):
    store = ReconcileStore(str(tmp_path))
    service = ReconcileService(store, now=1_760_000_000.0)
    profiles, samples = demo_dataset("2026-09")
    first = service.run("2026-09", profiles, samples, source="a")
    second = service.run("2026-09", profiles, samples, source="b")
    assert first.run_id != second.run_id
    records = store.list_runs("2026-09")
    assert [item["run_no"] for item in records] == [0, 1]
    assert store.latest_run("2026-09")["run"]["run_id"] == second.run_id
    # 第一批内容原样保留。
    assert records[0]["run"]["run_id"] == first.run_id
    report = service.verify()
    assert report["chain"]["ok"] is True
    assert all(item["ok"] for item in report["runs"])


def test_difference_report_matches_totals_and_console_and_json(tmp_path):
    store = ReconcileStore(str(tmp_path))
    service = ReconcileService(store, now=1_760_000_000.0)
    profiles, samples = demo_dataset("2026-09")
    run = service.run("2026-09", profiles, samples)

    diff = difference_report(run)
    totals = run.totals()
    assert diff["registered_kwh"] == totals["registered_kwh"]
    assert diff["booked_kwh"] == totals["booked_kwh"]
    assert diff["unbooked_register_kwh"] == totals["unbooked_register_kwh"]
    # 恒等式：寄存器读数差 = 实读 + 读数差类补入 + 标缺披露；
    # 冻住插值是表计修正（读数差为 0），不占读数差恒等式，需扣除。
    assert (
        diff["observed_kwh"] + diff["filled_kwh"] - diff["meter_correction_kwh"]
        + diff["unbooked_register_kwh"]
        == pytest.approx(diff["registered_kwh"])
    )

    payload = run_payload(run)
    assert payload["totals"]["booked_kwh"] == diff["booked_kwh"]
    assert payload["difference"]["registered_kwh"] == diff["registered_kwh"]

    text = render_console(run)
    assert "2026-09" in text
    assert "PARTIAL" in text
    # 文本里的入账合计数字与 JSON 同源（千分位格式）。
    assert f"{diff['booked_kwh']:,.2f}" in text
    # 三种已补原因和三类标缺原因都能在明细里出现。
    reasons = {segment.reason for meter in run.meters for segment in meter.segments}
    assert RATE_TRAPEZOID in reasons
    assert TOTAL_REGISTER_ALLOC in reasons
    assert TOTAL_NEIGHBOR_INTERP in reasons
    assert MISSING_LONG in reasons
    assert MISSING_CONDITION in reasons


def test_demo_dataset_covers_all_scenarios_and_identity_closes(tmp_path):
    profiles, samples = demo_dataset("2026-09")
    service = ReconcileService(ReconcileStore(str(tmp_path / "store")), now=1.0)
    run = service.run("2026-09", profiles, samples)
    totals = run.totals()
    # 有已补段、有标缺段、有回绕断链；部分失败为真。
    assert totals["filled_segments"] >= 4
    assert totals["missing_segments"] >= 3
    assert totals["ambiguous_breaks"] == 1
    # 批级闭合：registered = observed + 读数差类补入 + 标缺披露。
    assert (
        totals["observed_kwh"] + totals["filled_kwh"] - totals["meter_correction_kwh"]
        + totals["unbooked_register_kwh"]
        == pytest.approx(totals["registered_kwh"])
    )


def test_csv_roundtrip(tmp_path):
    from heatnet.recon.service import demo_csv

    raw_text, profile_text = demo_csv("2026-09")
    raw_path = tmp_path / "raw.csv"
    profile_path = tmp_path / "profiles.csv"
    raw_path.write_text(raw_text, encoding="utf-8")
    profile_path.write_text(profile_text, encoding="utf-8")

    samples, names = load_csv(str(raw_path))
    profiles = load_profiles(str(profile_path))
    assert names == ["R-1001", "T-2001", "T-2002"]
    assert {p.meter for p in profiles} == set(names)
    assert {p.kind for p in profiles} == {"rate", "total"}
    assert len(samples) > 0
    with pytest.raises(ValueError):
        load_csv(io.StringIO("meter,energy_kwh\nT,1\n"))
    with pytest.raises(ValueError):
        load_csv(io.StringIO("ts,meter,energy_kwh\nnot-a-time,T,1\n"))


def test_missing_conditions_never_produce_energy_numbers(tmp_path):
    """任何标缺段都不许带热量值；任何已补段都必须带热量值（不硬凑的结构性保证）。"""

    profiles, samples = demo_dataset("2026-09")
    store = ReconcileStore(str(tmp_path))
    run = ReconcileService(store, now=1.0).run("2026-09", profiles, samples)
    for meter in run.meters:
        for segment in meter.segments:
            if segment.is_filled():
                assert segment.filled_kwh is not None
            else:
                assert segment.filled_kwh is None
                assert segment.slot_kwh == () or all(v is None for v in segment.slot_kwh)
    # 落盘后的 JSON 也必须保持同样不变量。
    on_disk = store.latest_run("2026-09")["run"]
    for meter in on_disk["meters"]:
        for segment in meter["segments"]:
            if segment["status"] == "MISSING":
                assert segment["filled_kwh"] is None
