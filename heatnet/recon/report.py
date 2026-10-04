"""补账差异说明、总账报告与控制台文本渲染。

三个出口（控制台文本、HTTP JSON、审计落盘）全部从同一个
:class:`~heatnet.recon.state.ReconciliationRun` 派生：差异说明只解释总账数字是
怎么来的，不另算一份。任何展示都不得把标缺段写成热量值。
"""

from __future__ import annotations

import datetime as _dt
import json
import unicodedata

from .state import (
    AMBIGUOUS_ROLLOVER,
    BAD_SAMPLE,
    FILLED_REASONS,
    MISSING_CONDITION,
    MISSING_LONG,
    MISSING_NEIGHBOR,
    MISSING_REASONS,
    NO_ANCHOR,
    RATE_CLOSURE_MISMATCH,
    RATE_TRAPEZOID,
    ReconciliationRun,
    TOTAL_NEIGHBOR_INTERP,
    TOTAL_REGISTER_ALLOC,
)

REASON_TEXT = {
    RATE_TRAPEZOID: "流量温差反算(梯形积分)",
    TOTAL_REGISTER_ALLOC: "回绕解绕读数差分配",
    TOTAL_NEIGHBOR_INTERP: "相邻时段增量插值(读数冻住)",
    MISSING_LONG: "缺口超过长档阈值,标缺不补",
    MISSING_CONDITION: "缺流量/温差反算条件,标缺",
    MISSING_NEIGHBOR: "插值缺合格相邻时段,标缺",
    RATE_CLOSURE_MISMATCH: "反算与读数差对账超容差,标缺",
    AMBIGUOUS_ROLLOVER: "回绕次数无法唯一确定,断链标缺",
    BAD_SAMPLE: "读数跳变越限,坏点隔离",
    NO_ANCHOR: "月窗边缘无读数锚点,标缺",
}

TIER_TEXT = {"short": "短档", "mid": "中档", "long": "长档"}


def _iso(ts: float) -> str:
    return _dt.datetime.fromtimestamp(ts, tz=_dt.timezone.utc).strftime("%Y-%m-%d %H:%M")


def _display_width(text: str) -> int:
    """东亚宽字符按两列计宽，保证中文表格对齐。"""

    width = 0
    for char in text:
        width += 2 if unicodedata.east_asian_width(char) in ("W", "F") else 1
    return width


def _pad(text: str, width: int) -> str:
    return text + " " * max(0, width - _display_width(text))


def _num(value: float | None, digits: int = 2) -> str:
    return "—" if value is None else f"{value:,.{digits}f}"


def meter_difference_lines(run: ReconciliationRun, meter: str) -> list[dict]:
    """按段给出差异说明：读数差、补入量、未入账披露、残差，逐条可核对。"""

    result = next(item for item in run.meters if item.meter == meter)
    lines: list[dict] = []
    for segment in result.segments:
        lines.append(
            {
                "start": _iso(segment.start_ts),
                "end": _iso(segment.end_ts),
                "missing_slots": segment.missing_slots,
                "tier": segment.tier,
                "status": "FILLED" if segment.is_filled() else "MISSING",
                "reason": segment.reason,
                "reason_text": REASON_TEXT.get(segment.reason, segment.reason),
                "register_delta_kwh": segment.register_delta_kwh,
                "filled_kwh": segment.filled_kwh,
                "residual_kwh": segment.residual_kwh,
                "wraps": segment.wraps,
            }
        )
    return lines


def difference_report(run: ReconciliationRun) -> dict:
    """差异说明总账：把"月底结算为什么少一截"拆成可核对的几部分。

    * registered        连续锚点间寄存器读数差合计（理论应收口径）；
    * observed          不用补、直接读到的增量；
    * filled            断联段已补并入账的估算增量；
    * correction        total 表读数冻住时靠旁证补入的表计修正（读数差为 0）；
    * unbooked_register 标缺段里读数差能量化但按口径不允许入账的部分（长档挂账、
      对账失败、缺条件段等的披露量）；
    * missing_only      无法量化的标缺段数量/时长（回绕多解、长档等）；
    * residual          rate 反算原始量与读数差的残差（披露，不改入账）。
    """

    totals = run.totals()
    missing_segments: list[dict] = []
    duration_s = 0.0
    for meter in run.meters:
        for segment in meter.segments:
            if not segment.is_filled():
                missing_segments.append(
                    {
                        "meter": meter.meter,
                        "start": _iso(segment.start_ts),
                        "end": _iso(segment.end_ts),
                        "reason": segment.reason,
                        "reason_text": REASON_TEXT.get(segment.reason, segment.reason),
                        "tier": segment.tier,
                        "register_delta_kwh": segment.register_delta_kwh,
                    }
                )
                duration_s += segment.end_ts - segment.start_ts
    return {
        "month": run.month,
        "run_id": run.run_id,
        "registered_kwh": totals["registered_kwh"],
        "observed_kwh": totals["observed_kwh"],
        "filled_kwh": totals["filled_kwh"],
        "booked_kwh": totals["booked_kwh"],
        "meter_correction_kwh": totals["meter_correction_kwh"],
        "unbooked_register_kwh": totals["unbooked_register_kwh"],
        "missing_gap_kwh_disclosed": totals["missing_gap_kwh"],
        "residual_kwh_disclosed": totals["residual_kwh"],
        "no_anchor_slots": totals["no_anchor_slots"],
        "ambiguous_breaks": totals["ambiguous_breaks"],
        "missing_segments": missing_segments,
        "missing_window_hours": round(duration_s / 3600.0, 2),
        "partial_failure": run.partial_failure,
    }


def run_payload(run: ReconciliationRun) -> dict:
    """控制台/审计共用的完整 JSON：补账段 + 差异说明 + 总账，一份数。"""

    payload = run.as_dict()
    payload["difference"] = difference_report(run)
    payload["meters_diff"] = {meter.meter: meter_difference_lines(run, meter.meter) for meter in run.meters}
    return payload


# ---- 文本表格（控制台展示） ---------------------------------------------------


def render_console(run: ReconciliationRun) -> str:
    """渲染控制台文本：补账结果、差异说明、总账，三部分同源。"""

    lines: list[str] = []
    totals = run.totals()
    lines.append(f"断联补账报告  月份={run.month}  批次={run.run_id}")
    lines.append(
        "口径: 按表型定方法(rate=流量温差反算 / total=读数差分配,短档冻住才插值); "
        "按阈值分档(短≤2槽, 中≤24h, 长标缺); 历史只追加不改写"
    )
    if run.partial_failure:
        lines.append("状态: PARTIAL —— 存在标缺段，缺条件的一段未硬凑，结算前请人工确认")
    else:
        lines.append("状态: OK —— 全部断联段已补，账已闭合")
    lines.append("")

    header = ("表号", "表型", "实读kWh", "补入kWh", "入账kWh", "挂账披露kWh", "反算残差", "已补段", "标缺段")
    rows = [header]
    for meter in run.meters:
        rows.append(
            (
                meter.meter,
                meter.kind,
                _num(meter.observed_kwh),
                _num(meter.filled_kwh),
                _num(meter.booked_kwh),
                _num(meter.missing_gap_kwh),
                _num(meter.residual_kwh),
                str(meter.filled_segments),
                str(meter.missing_segments),
            )
        )
    lines.append(_table(rows))
    lines.append("")

    lines.append("补账/标缺明细")
    detail_header = ("表号", "起", "止", "缺槽", "档", "状态", "原因", "读数差", "补入")
    detail_rows = [detail_header]
    for meter in run.meters:
        for segment in meter.segments:
            detail_rows.append(
                (
                    meter.meter,
                    _iso(segment.start_ts)[5:],
                    _iso(segment.end_ts)[5:],
                    str(segment.missing_slots),
                    TIER_TEXT.get(segment.tier, segment.tier),
                    "已补" if segment.is_filled() else "标缺",
                    REASON_TEXT.get(segment.reason, segment.reason),
                    _num(segment.register_delta_kwh, 1),
                    _num(segment.filled_kwh, 1),
                )
            )
    lines.append(_table(detail_rows) if len(detail_rows) > 1 else "  （无断联段）")
    lines.append("")

    diff = difference_report(run)
    lines.append("差异说明（kWh）")
    for label, key in (
        ("寄存器读数差合计(理论应收)", "registered_kwh"),
        ("  其中: 直接实读", "observed_kwh"),
        ("  其中: 断联补入(估算)", "filled_kwh"),
        ("  其中: 标缺段读数差披露,未入账", "unbooked_register_kwh"),
        ("表计修正(读数冻住,旁证补入)", "meter_correction_kwh"),
        ("反算残差披露(不改账)", "residual_kwh_disclosed"),
    ):
        lines.append(f"  {label}: {_num(diff[key])}")
    lines.append(f"  入账合计(实读+补入): {_num(diff['booked_kwh'])}")
    lines.append(
        f"  无法量化标缺段: {totals['missing_segments']} 段, "
        f"约 {diff['missing_window_hours']:.1f} 小时; 回绕断链 {totals['ambiguous_breaks']} 处; "
        f"月窗边缘无锚点 {totals['no_anchor_slots']} 槽"
    )
    return "\n".join(lines)


def _table(rows: list[tuple[str, ...]]) -> str:
    columns = len(rows[0])
    widths = [0] * columns
    for row in rows:
        for index, cell in enumerate(row):
            widths[index] = max(widths[index], _display_width(str(cell)))
    rendered = []
    for row_index, row in enumerate(rows):
        rendered.append("  ".join(_pad(str(cell), widths[index]) for index, cell in enumerate(row)))
        if row_index == 0:
            rendered.append("  ".join("-" * widths[index] for index in range(columns)))
    return "\n".join(rendered)


def to_json(run: ReconciliationRun, indent: int = 2) -> str:
    return json.dumps(run_payload(run), ensure_ascii=False, indent=indent)
