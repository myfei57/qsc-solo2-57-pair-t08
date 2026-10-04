"""补账服务：导入原始采样 → 执行补账 → 追加落账 → 审计留痕。

原始 CSV 一旦归档不改写；重复导入同月原始数据会记 ``raw.imported`` 审计但不会
修改任何旧批次。补账批次只追加，月度结算取该月最新批次。
"""

from __future__ import annotations

import csv
import datetime as _dt
import io
import os
import time

from .engine import ConsistencyError, reconcile
from .state import DEFAULT_POLICY, MeterProfile, Policy, ReconciliationRun, Sample
from .store import ReconcileStore

REQUIRED_COLUMNS = ("ts", "meter", "energy_kwh")
OPTIONAL_COLUMNS = ("flow_t_h", "supply_c", "return_c")


def parse_ts(value: str) -> float:
    """解析采样时间：支持 ISO8601（含 ``Z``）与 epoch 秒。"""

    text = value.strip()
    try:
        return float(text)
    except ValueError:
        pass
    normalized = text.replace("Z", "+00:00")
    try:
        parsed = _dt.datetime.fromisoformat(normalized)
    except ValueError as error:
        raise ValueError(f"无法解析采样时间 {value!r}，需要 ISO8601 或 epoch 秒") from error
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_dt.timezone.utc)
    return parsed.timestamp()


def _as_float(value: str | None) -> float | None:
    if value is None:
        return None
    text = str(value).strip()
    if text == "" or text.lower() in ("na", "null", "none"):
        return None
    return float(text)


def load_csv(source: "str | io.IOBase") -> tuple[list[Sample], list[str]]:
    """读取原始采样 CSV，返回样本与出现过的表名（按出现顺序）。

    入参可以是文件路径、文件对象，也可以直接是 CSV 文本。
    """

    if isinstance(source, io.IOBase):
        return _parse_csv(source.read())
    if isinstance(source, (str, bytes, os.PathLike)) and os.path.isfile(source):
        with open(source, "r", encoding="utf-8", newline="") as handle:
            return _parse_csv(handle.read())
    return _parse_csv(str(source))


def _parse_csv(text: str) -> tuple[list[Sample], list[str]]:
    reader = csv.DictReader(io.StringIO(text))
    if reader.fieldnames is None or not set(REQUIRED_COLUMNS).issubset(reader.fieldnames):
        raise ValueError(f"CSV 必须包含列 {REQUIRED_COLUMNS}")
    samples: list[Sample] = []
    names: list[str] = []
    for line, row in enumerate(reader, start=2):
        meter = (row.get("meter") or "").strip()
        if not meter:
            raise ValueError(f"第 {line} 行缺少 meter")
        try:
            sample = Sample(
                ts=parse_ts(row["ts"]),
                meter=meter,
                energy_kwh=_as_float(row.get("energy_kwh")),
                flow_t_h=_as_float(row.get("flow_t_h")),
                supply_c=_as_float(row.get("supply_c")),
                return_c=_as_float(row.get("return_c")),
            )
        except (TypeError, ValueError) as error:
            raise ValueError(f"第 {line} 行数值非法: {error}") from error
        samples.append(sample)
        if meter not in names:
            names.append(meter)
    return samples, names


def load_profiles(path: str) -> list[MeterProfile]:
    """读取表型配置 CSV：meter,kind,wrap_kwh,period_s,k_factor,max_power_kw。"""

    with open(path, "r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        required = {"meter", "kind", "wrap_kwh", "period_s"}
        if reader.fieldnames is None or not required.issubset(reader.fieldnames):
            raise ValueError(f"表型配置必须包含列 {sorted(required)}")
        profiles: list[MeterProfile] = []
        seen: set[str] = set()
        for line, row in enumerate(reader, start=2):
            meter = (row.get("meter") or "").strip()
            if meter in seen:
                raise ValueError(f"表型配置第 {line} 行表号重复: {meter}")
            seen.add(meter)
            profiles.append(
                MeterProfile(
                    meter=meter,
                    kind=(row.get("kind") or "").strip(),
                    wrap_kwh=float(row["wrap_kwh"]),
                    period_s=float(row["period_s"]),
                    k_factor=float(row.get("k_factor") or 1.163),
                    max_power_kw=_as_float(row.get("max_power_kw")),
                )
            )
    return profiles


class ReconcileService:
    """补账应用服务，绑定一个只追加的 :class:`ReconcileStore`。"""

    def __init__(self, store: ReconcileStore, now: float | None = None) -> None:
        self._store = store
        self._now = float(now) if now is not None else time.time()

    @property
    def store(self) -> ReconcileStore:
        return self._store

    def import_raw(self, source: str, samples: list[Sample]) -> dict:
        """归档一批原始采样并记审计。重复导入不改旧数据。"""

        count = self._store.append_raw(source, samples)
        self._store.append_audit(
            "raw.imported",
            severity="notice",
            message=f"归档原始采样 {count} 条（来源 {source}，原始数据不可变）",
            source=source,
            count=count,
        )
        return {"source": source, "count": count}

    def run(
        self,
        month: str,
        profiles: list[MeterProfile],
        samples: list[Sample],
        *,
        archive_raw: bool = True,
        source: str = "cli",
    ) -> ReconciliationRun:
        """执行一次月度补账：归档原始数据 → 补账 → 追加总账 → 审计留痕。"""

        if archive_raw:
            self.import_raw(source, samples)
        run_no = self._store.ledger_journal.count()
        # run_id 带毫秒时间戳和批次序号：同一毫秒内重跑也不撞号（批次只追加）。
        run_id = f"recon-{month}-{int(self._now * 1000)}-{run_no:04d}"
        run = reconcile(
            month, profiles, samples, Policy(), created_ts=self._now, run_id=run_id
        )
        record = self._store.append_run(run)
        totals = run.totals()
        self._store.append_audit(
            "recon.run",
            run_id=run_id,
            month=month,
            severity="warning" if run.partial_failure else "notice",
            message=(
                f"{month} 补账完成: 入账 {totals['booked_kwh']:.3f} kWh "
                f"(实读 {totals['observed_kwh']:.3f} + 补入 {totals['filled_kwh']:.3f}), "
                f"标缺 {totals['missing_segments']} 段, 挂账披露 "
                f"{totals['unbooked_register_kwh']:.3f} kWh"
                + ("；部分失败：缺条件段未硬凑" if run.partial_failure else "")
            ),
            run_no=record["run_no"],
            raw_sample_count=run.raw_sample_count,
            raw_sha256=run.raw_sha256,
            **{f"total_{key}": value for key, value in totals.items()},
        )
        for meter in run.meters:
            for segment in meter.segments:
                if not segment.is_filled():
                    self._store.append_audit(
                        "recon.segment_missing",
                        run_id=run_id,
                        month=month,
                        severity="warning",
                        message=(
                            f"表 {meter.meter} 段 {_iso(segment.start_ts)}~{_iso(segment.end_ts)} "
                            f"标缺({segment.reason})，缺槽 {segment.missing_slots}"
                        ),
                        meter=meter.meter,
                        reason=segment.reason,
                        tier=segment.tier,
                        missing_slots=segment.missing_slots,
                        register_delta_kwh=segment.register_delta_kwh,
                    )
        self._store.append_audit(
            "recon.policy",
            run_id=run_id,
            month=month,
            message="补账口径: 按表型定方法 + 按阈值分档(非回绕前一律补满) + 历史只追加",
            **DEFAULT_POLICY.as_dict(),
        )
        return run

    def latest(self, month: str) -> dict | None:
        return self._store.latest_run(month)

    def raw_records(self) -> list[dict]:
        """回放全部已归档的原始采样（只读，原始数据永不改写）。"""

        return self._store.read_raw()

    def verify(self) -> dict:
        """校验审计哈希链，并复核最新账与总账的一致性。"""

        chain = self._store.verify_chain()
        checked: list[dict] = []
        for record in self._store.list_runs():
            payload = record["run"]
            totals = payload["totals"]
            recomputed = sum(meter["booked_kwh"] for meter in payload["meters"])
            if abs(recomputed - totals["booked_kwh"]) > 1e-6:
                chain = {"ok": False, "broken_at": record["run_no"], "reason": "booked_total_mismatch"}
            checked.append(
                {"run_no": record["run_no"], "month": payload["month"], "ok": chain["ok"]}
            )
        return {"chain": chain, "runs": checked}


def _iso(ts: float) -> str:
    return _dt.datetime.fromtimestamp(ts, tz=_dt.timezone.utc).strftime("%m-%d %H:%M")


# ---- 演示数据 ------------------------------------------------------------------


def demo_dataset(month: str = "2026-09") -> tuple[list[MeterProfile], list[Sample]]:
    """构造一份覆盖各类情形的演示数据（每小时一槽，整月网格）。

    * T-2001（total, wrap=1000）：4 槽断联正好跨过回绕点（唯一解绕，读数差
      分配）、1 槽断联期间寄存器冻住但流量温差显示在用热（短档邻居插值修正）；
    * T-2002（total, wrap=200）：30 槽长断联，恢复读数使 0/1/2… 次回绕都落在
      功率限幅窗口内（回绕多解，断链标缺）。
    """

    from .engine import month_window

    start, end = month_window(month)
    period_s = 3600.0
    n = int(round((end - start) / period_s))

    profiles = [
        MeterProfile("R-1001", "rate", wrap_kwh=1_000_000.0, period_s=period_s,
                     max_power_kw=40.0),
        MeterProfile("T-2001", "total", wrap_kwh=1000.0, period_s=period_s,
                     max_power_kw=60.0),
        MeterProfile("T-2002", "total", wrap_kwh=200.0, period_s=period_s,
                     max_power_kw=25.0),
    ]
    samples: dict[tuple[str, int], Sample] = {}

    def add(slot: int, sample: Sample) -> None:
        samples[(sample.meter, slot)] = sample

    def power_at(slot: int) -> float:
        # 周期三角波（周期 12h，8~20kW）：逐槽线性，梯形积分逐段精确。
        phase = slot % 12
        triangle = phase if phase <= 6 else 12 - phase
        return 8.0 + 2.0 * triangle

    # ---- R-1001：寄存器增量按功率梯形积分精确累计 ----
    energy = 0.0
    skip_slots = set(range(200, 230))     # 30 槽长断联（槽 200~229 缺采样）
    skip_slots.update(range(97, 100))     # 3 槽中断（槽 97~99，功率单调段，可反算）
    skip_slots.add(300)                    # 1 槽短断联，恢复帧缺分量 → 缺条件
    for slot in range(n):
        ts = start + slot * period_s
        if slot > 0:
            energy += (power_at(slot - 1) + power_at(slot)) / 2.0
        flow = 30.0
        dt_c = power_at(slot) / (1.163 * flow)
        if slot in skip_slots:
            continue
        if slot == 400:
            # 读数跳变越限：分量正常，但累计读数凭空大 5000 kWh → 坏点隔离。
            add(slot, Sample(ts, "R-1001", energy + 5000.0, flow, 80.0, 80.0 - dt_c))
            continue
        if slot == 301:
            # 恢复帧只带读数、不带分量：与 300 槽缺失构成"缺条件"短段。
            add(slot, Sample(ts, "R-1001", energy, None, None, None))
            continue
        add(slot, Sample(ts, "R-1001", energy, flow, 80.0, 80.0 - dt_c))

    # ---- T-2001：30 kWh/槽，回绕 1000；安排跨回绕的 4 槽中档断联 ----
    per_slot = 30.0
    # 槽18 raw=970；槽23 raw=120：唯一解绕为 970→1120（跨一次回绕，读数差150）。
    offset1 = 970.0 - 18 * per_slot
    t1_skip = set(range(19, 23))       # 4 槽断联（19~22），跨回绕
    # 槽 300 断联期间寄存器冻住；恢复后表从 400 重新计，永久滞后真实值 60 kWh。
    frozen_raw = (offset1 + 299 * per_slot) % 1000.0
    for slot in range(n):
        ts = start + slot * period_s
        if slot in t1_skip or slot == 300:
            continue
        if slot == 301:
            # 读数与槽299相同（冻住，读数差为0）；流量温差显示约 35 kW 在用热。
            add(slot, Sample(ts, "T-2001", frozen_raw, 2.0, 75.0, 59.93))
            continue
        if slot >= 302:
            raw = (frozen_raw + (slot - 301) * per_slot) % 1000.0
        else:
            raw = (offset1 + slot * per_slot) % 1000.0
        add(slot, Sample(ts, "T-2001", raw, None, None, None))

    # ---- T-2002：20 kWh/槽，回绕 200；30 槽长断联且回绕多解 ----
    per_slot2 = 20.0
    t2_gap = set(range(200, 230))
    # 槽199 raw=180；槽230 raw=0。功率上限允许 25kW×31槽=775 kWh，候选
    # 4000/4200/4400/4600（差 20/220/420/620）全部落在窗口 → 回绕次数多解。
    for slot in range(n):
        ts = start + slot * period_s
        if slot in t2_gap:
            continue
        add(slot, Sample(ts, "T-2002", (slot * per_slot2) % 200.0, None, None, None))

    return profiles, [samples[key] for key in sorted(samples, key=lambda item: (item[1], item[0]))]


def demo_csv(month: str = "2026-09") -> tuple[str, str]:
    """生成演示用原始采样 CSV 与表型配置 CSV 文本。"""

    profiles, samples = demo_dataset(month)
    raw_buffer = io.StringIO()
    writer = csv.writer(raw_buffer)
    writer.writerow(["ts", "meter", "energy_kwh", "flow_t_h", "supply_c", "return_c"])
    for sample in samples:
        writer.writerow(
            [
                _dt.datetime.fromtimestamp(sample.ts, tz=_dt.timezone.utc)
                .strftime("%Y-%m-%dT%H:%M:%SZ"),
                sample.meter,
                "" if sample.energy_kwh is None else round(sample.energy_kwh, 3),
                "" if sample.flow_t_h is None else sample.flow_t_h,
                "" if sample.supply_c is None else sample.supply_c,
                "" if sample.return_c is None else sample.return_c,
            ]
        )
    profile_buffer = io.StringIO()
    writer = csv.writer(profile_buffer)
    writer.writerow(["meter", "kind", "wrap_kwh", "period_s", "k_factor", "max_power_kw"])
    for profile in profiles:
        writer.writerow(
            [
                profile.meter,
                profile.kind,
                profile.wrap_kwh,
                profile.period_s,
                profile.k_factor,
                "" if profile.max_power_kw is None else profile.max_power_kw,
            ]
        )
    return raw_buffer.getvalue(), profile_buffer.getvalue()
