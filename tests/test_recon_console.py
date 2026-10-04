"""断联补账的控制台接口：执行、查询、审计、校验。"""

from __future__ import annotations

from heatnet.console.api import Api


def test_recon_demo_endpoint_runs_and_reports_partial(platform):
    api = Api(platform)
    status, payload = api.handle("POST", "/api/recon/demo", {}, {"month": "2026-09"})
    assert status == 200
    assert payload["month"] == "2026-09"
    assert payload["partial_failure"] is True
    totals = payload["totals"]
    assert totals["filled_segments"] >= 4
    assert totals["missing_segments"] >= 3
    # 差异说明和总账在同一个响应体里，数字同源。
    assert payload["difference"]["booked_kwh"] == totals["booked_kwh"]
    # 标缺段不带热量值。
    for meter in payload["meters"]:
        for segment in meter["segments"]:
            if segment["status"] == "MISSING":
                assert segment["filled_kwh"] is None


def test_recon_runs_latest_audit_and_verify(platform):
    api = Api(platform)
    status, _ = api.handle("POST", "/api/recon/demo", {}, {"month": "2026-09"})
    assert status == 200

    status, runs = api.handle("GET", "/api/recon/runs", {}, {})
    assert status == 200
    assert runs["count"] == 1

    status, latest = api.handle("GET", "/api/recon/latest", {"month": "2026-09"}, {})
    assert status == 200
    assert latest["found"] is True
    assert latest["record"]["run"]["month"] == "2026-09"

    status, audit = api.handle("GET", "/api/recon/audit", {"limit": "10"}, {})
    assert status == 200
    actions = {entry["action"] for entry in audit["entries"]}
    assert "recon.run" in actions
    assert "recon.policy" in actions

    status, verify = api.handle("GET", "/api/recon/verify", {}, {})
    assert status == 200
    assert verify["chain"]["ok"] is True


def test_recon_latest_unknown_month_and_bad_inputs(platform):
    api = Api(platform)
    status, payload = api.handle("GET", "/api/recon/latest", {"month": "2000-01"}, {})
    assert status == 200
    assert payload["found"] is False

    status, payload = api.handle("POST", "/api/recon/run", {}, {"month": "2026-09"})
    assert status == 409
    assert payload["component"] == "recon"

    status, payload = api.handle("POST", "/api/recon/run", {}, {})
    assert status == 409


def test_recon_run_endpoint_with_profiles_and_samples(platform):
    from heatnet.recon.engine import month_window

    start, _ = month_window("2026-09")
    hour = 3600.0
    body = {
        "month": "2026-09",
        "profiles": [
            {"meter": "X", "kind": "total", "wrap_kwh": 1000.0, "period_s": hour,
             "max_power_kw": 100.0}
        ],
        "samples": [
            {"ts": start, "meter": "X", "energy_kwh": 0.0},
            {"ts": start + 4 * hour, "meter": "X", "energy_kwh": 40.0},
        ],
    }
    api = Api(platform)
    status, payload = api.handle("POST", "/api/recon/run", {}, body)
    assert status == 200
    meter = payload["meters"][0]
    assert meter["meter"] == "X"
    assert meter["filled_segments"] == 1
    assert meter["filled_kwh"] == 40.0
