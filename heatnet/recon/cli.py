"""断联补账命令行。

``recon run``     导入原始采样 CSV + 表型配置，执行月度补账，落追加式总账与审计，
                  同时打印控制台文本（``--json`` 输出同源 JSON）；
``recon latest``  读取某月最新一批补账；
``recon list``    列出全部补账批次（历史不可变，只追加）；
``recon audit``   查看补账审计流水；
``recon verify``  校验审计哈希链与总账一致性；
``recon demo``    生成演示 CSV（覆盖长档标缺、跨回绕解绕、冻住插值、回绕多解）。

存在标缺段属于部分失败：命令仍正常退出（exit 0），报告以 PARTIAL 明示；审计链
断裂或总账不闭合属于系统错误，exit 2。
"""

from __future__ import annotations

import argparse
import json
import os
import sys

from .report import render_console, run_payload
from .service import (
    ReconcileService,
    demo_csv,
    load_csv,
    load_profiles,
)
from .state import ReconciliationRun
from .store import ReconcileStore

_DEFAULT_ROOT = os.environ.get("HEATNET_STORAGE_ROOT", "./var/heatnet-recon")


def _build_service(args: argparse.Namespace) -> ReconcileService:
    root = getattr(args, "store_root", None) or _DEFAULT_ROOT
    return ReconcileService(ReconcileStore(root))


def _run_from_record(record: dict) -> ReconciliationRun:
    """从落盘字典重建运行对象（只读，不重新计算）。"""

    from .state import BadSlot, MeterResult, Policy, Segment

    payload = record["run"]
    meters = []
    for meter in payload["meters"]:
        meters.append(
            MeterResult(
                meter=meter["meter"],
                kind=meter["kind"],
                wrap_kwh=meter["wrap_kwh"],
                period_s=meter["period_s"],
                segments=tuple(Segment(**{k: v for k, v in segment.items()
                                          if k in Segment.__dataclass_fields__})
                               for segment in meter["segments"]),
                bad_slots=tuple(BadSlot(**slot) for slot in meter["bad_slots"]),
                anchors=tuple((anchor["ts"], anchor["raw_kwh"], anchor["unwrapped_kwh"])
                              for anchor in meter["anchors"]),
                observed_kwh=meter["observed_kwh"],
                filled_kwh=meter["filled_kwh"],
                registered_kwh=meter["registered_kwh"],
                missing_gap_kwh=meter["missing_gap_kwh"],
                residual_kwh=meter["residual_kwh"],
                correction_kwh=meter.get("correction_kwh", 0.0),
                missing_segments=meter["missing_segments"],
                filled_segments=meter["filled_segments"],
                ambiguous_breaks=meter["ambiguous_breaks"],
                no_anchor_slots=meter["no_anchor_slots"],
                max_power_kw=meter.get("max_power_kw"),
            )
        )
    policy = Policy(**{k: v for k, v in payload["policy"].items() if k in Policy.__dataclass_fields__})
    return ReconciliationRun(
        run_id=payload["run_id"],
        month=payload["month"],
        window_start_ts=payload["window_start_ts"],
        window_end_ts=payload["window_end_ts"],
        created_ts=payload["created_ts"],
        policy=policy,
        meters=tuple(meters),
        raw_sample_count=payload["raw_sample_count"],
        raw_sha256=payload["raw_sha256"],
        partial_failure=payload["partial_failure"],
    )


def _cmd_run(args: argparse.Namespace) -> int:
    service = _build_service(args)
    samples, _ = load_csv(args.raw_csv)
    profiles = load_profiles(args.profiles)
    known = {profile.meter for profile in profiles}
    unknown = sorted({sample.meter for sample in samples} - known)
    if unknown:
        print(f"原始数据里有未登记表型的表: {unknown}", file=sys.stderr)
        return 2
    run = service.run(args.month, profiles, samples, source="cli")
    if args.json:
        print(json.dumps(run_payload(run), ensure_ascii=False, indent=2))
    else:
        print(render_console(run))
    return 0


def _cmd_latest(args: argparse.Namespace) -> int:
    service = _build_service(args)
    record = service.latest(args.month)
    if record is None:
        print(json.dumps({"month": args.month, "found": False}, ensure_ascii=False))
        return 1
    run = _run_from_record(record)
    if args.json:
        print(json.dumps(run_payload(run), ensure_ascii=False, indent=2))
    else:
        print(render_console(run))
    return 0


def _cmd_list(args: argparse.Namespace) -> int:
    service = _build_service(args)
    rows = []
    for item in service.store.list_runs():
        payload = item["run"]
        totals = payload["totals"]
        rows.append(
            {
                "run_no": item["run_no"],
                "run_id": payload["run_id"],
                "month": payload["month"],
                "created_ts": payload["created_ts"],
                "booked_kwh": totals["booked_kwh"],
                "filled_kwh": totals["filled_kwh"],
                "missing_segments": totals["missing_segments"],
                "partial_failure": payload["partial_failure"],
            }
        )
    print(json.dumps({"runs": rows, "count": len(rows)}, ensure_ascii=False, indent=2))
    return 0


def _cmd_audit(args: argparse.Namespace) -> int:
    service = _build_service(args)
    entries = service.store.audit_entries()
    if args.month:
        entries = [entry for entry in entries if entry.get("month") == args.month]
    print(json.dumps({"entries": entries[-args.limit :]}, ensure_ascii=False, indent=2))
    return 0


def _cmd_verify(args: argparse.Namespace) -> int:
    service = _build_service(args)
    report = service.verify()
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["chain"]["ok"] else 2


def _cmd_raw(args: argparse.Namespace) -> int:
    service = _build_service(args)
    records = service.raw_records()
    print(json.dumps({"count": len(records), "records": records[-args.limit :]},
                     ensure_ascii=False, indent=2))
    return 0


def _cmd_demo(args: argparse.Namespace) -> int:
    os.makedirs(args.out_dir, exist_ok=True)
    raw_text, profile_text = demo_csv(args.month)
    raw_path = os.path.join(args.out_dir, f"raw-{args.month}.csv")
    profile_path = os.path.join(args.out_dir, "profiles.csv")
    with open(raw_path, "w", encoding="utf-8") as handle:
        handle.write(raw_text)
    with open(profile_path, "w", encoding="utf-8") as handle:
        handle.write(profile_text)
    print(json.dumps({"raw_csv": raw_path, "profiles_csv": profile_path}, ensure_ascii=False, indent=2))
    if args.run:
        argv = ["--store-root", args.store_root, "run", "--month", args.month,
                "--raw-csv", raw_path, "--profiles", profile_path]
        return main(argv)
    return 0


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    # 同名选项在顶层和子层都登记；默认值只能在 parse_args 后补，否则子层默认值
    # 会覆盖顶层显式传入的 --store-root。
    common.add_argument("--store-root", default=argparse.SUPPRESS, help="补账存储目录（只追加）")
    parser = argparse.ArgumentParser(prog="heatnet-recon", description="热量表断联补账", parents=[common])
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="执行月度补账并落审计", parents=[common])
    run.add_argument("--month", required=True, help="结算月 YYYY-MM")
    run.add_argument("--raw-csv", required=True, help="原始采样 CSV")
    run.add_argument("--profiles", required=True, help="表型配置 CSV")
    run.add_argument("--json", action="store_true", help="输出同源 JSON 而不是文本表")
    run.set_defaults(func=_cmd_run)

    latest = sub.add_parser("latest", help="查看某月最新补账", parents=[common])
    latest.add_argument("--month", required=True)
    latest.add_argument("--json", action="store_true")
    latest.set_defaults(func=_cmd_latest)

    listed = sub.add_parser("list", help="列出全部补账批次", parents=[common])
    listed.set_defaults(func=_cmd_list)

    audit = sub.add_parser("audit", help="查看补账审计流水", parents=[common])
    audit.add_argument("--month")
    audit.add_argument("--limit", type=int, default=50)
    audit.set_defaults(func=_cmd_audit)

    verify = sub.add_parser("verify", help="校验审计哈希链与总账一致性", parents=[common])
    verify.set_defaults(func=_cmd_verify)

    raw = sub.add_parser("raw", help="回放已归档的原始采样", parents=[common])
    raw.add_argument("--limit", type=int, default=20)
    raw.set_defaults(func=_cmd_raw)

    demo = sub.add_parser("demo", help="生成演示 CSV", parents=[common])
    demo.add_argument("--month", default="2026-09")
    demo.add_argument("--out-dir", default="./var/recon-demo")
    demo.add_argument("--run", action="store_true", help="生成后立即跑一遍补账")
    demo.set_defaults(func=_cmd_demo)
    return parser


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8", errors="backslashreplace")
    parser = build_parser()
    args = parser.parse_args(argv)
    if not hasattr(args, "store_root"):
        args.store_root = _DEFAULT_ROOT
    try:
        return int(args.func(args))
    except (ValueError, OSError) as error:
        print(json.dumps({"error": type(error).__name__, "message": str(error)},
                         ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
