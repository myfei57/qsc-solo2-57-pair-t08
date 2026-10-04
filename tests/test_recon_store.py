"""补账存储：只追加总账、月度取最新、审计哈希链与篡改检测。"""

from __future__ import annotations

import os

import json

import pytest

from heatnet.recon.engine import month_window, reconcile
from heatnet.recon.state import MeterProfile, Sample
from heatnet.recon.store import ReconcileStore

START, _ = month_window("2026-09")
HOUR = 3600.0
MONTH = "2026-09"


def total_sample(slot: int, raw: float) -> Sample:
    return Sample(START + slot * HOUR, "T", raw)


def _run(samples, *, created=1.0, run_id="r1"):
    profiles = [MeterProfile("T", "total", wrap_kwh=1000.0, period_s=HOUR, max_power_kw=100.0)]
    return reconcile(MONTH, profiles, samples, created_ts=created, run_id=run_id)


def test_raw_samples_are_appended_and_never_overwritten(tmp_path):
    store = ReconcileStore(str(tmp_path))
    samples = [total_sample(0, 10.0)]
    store.append_raw("csv-a", samples)
    store.append_raw("csv-b", samples)
    rows = store.read_raw()
    assert len(rows) == 2
    assert [row["source"] for row in rows] == ["csv-a", "csv-b"]
    # 原始记录的字段固定，不含补账加工结果。
    assert rows[0]["sample"]["energy_kwh"] == 10.0
    assert "filled" not in rows[0]["sample"]


def test_runs_are_append_only_and_latest_wins_per_month(tmp_path):
    store = ReconcileStore(str(tmp_path))
    run1 = _run([total_sample(0, 0.0), total_sample(1, 10.0)], created=1.0, run_id="r1")
    run2 = _run([total_sample(0, 0.0), total_sample(1, 12.0)], created=2.0, run_id="r2")
    store.append_run(run1)
    store.append_run(run2)
    records = store.list_runs()
    assert [item["run_no"] for item in records] == [0, 1]
    assert [item["run"]["run_id"] for item in records] == ["r1", "r2"]
    latest = store.latest_run(MONTH)
    assert latest["run"]["run_id"] == "r2"
    assert store.latest_run("1999-01") is None
    # 旧批次仍在，可回放审计：历史不可变，只有追加。
    assert records[0]["run"]["totals"]["booked_kwh"] != latest["run"]["totals"]["booked_kwh"]


def test_audit_hash_chain_detects_tampering(tmp_path):
    store = ReconcileStore(str(tmp_path))
    store.append_audit("raw.imported", message="one")
    store.append_audit("recon.run", run_id="r1", month=MONTH, booked=10.0)
    store.append_audit("recon.segment_missing", run_id="r1", month=MONTH, reason="MISSING_LONG")

    report = store.verify_chain()
    assert report["ok"] is True
    assert report["entries"] == 3

    # 篡改中间一条的载荷：自身哈希对不上。
    path = os.path.join(str(tmp_path), "audit-00000.jsonl")
    lines = open(path, encoding="utf-8").read().splitlines()
    body = json.loads(lines[1])
    body["message"] = "tampered"
    lines[1] = json.dumps(body, ensure_ascii=False)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")

    tampered = ReconcileStore(str(tmp_path))
    report = tampered.verify_chain()
    assert report["ok"] is False
    assert report["broken_at"] == 1


def test_audit_chain_detects_reorder_and_deletion(tmp_path):
    store = ReconcileStore(str(tmp_path))
    for index in range(3):
        store.append_audit("recon.policy", message=f"m{index}")
    path = os.path.join(str(tmp_path), "audit-00000.jsonl")
    lines = open(path, encoding="utf-8").read().splitlines()
    # 删掉中间一条：后续条目的 prev_sha256 全部断链。
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("\n".join([lines[0], lines[2]]) + "\n")
    report = ReconcileStore(str(tmp_path)).verify_chain()
    assert report["ok"] is False
    assert report["broken_at"] == 1


def test_audit_chain_self_hashes_cover_full_payload(tmp_path):
    store = ReconcileStore(str(tmp_path))
    store.append_audit("recon.run", run_id="r", month=MONTH, totals={"booked_kwh": 42.0})
    entry = store.audit_entries()[0]
    assert entry["prev_sha256"] is None
    assert entry["payload"]["totals"] == {"booked_kwh": 42.0}
    assert len(entry["sha256"]) == 64
