"""补账结果存储：原始采样只归档、补账总账只追加、审计哈希链。

三类落盘物口径不同，不能混写：

* ``raw-*.jsonl``   原始采样归档，追加且永不改写（补账以后原来那份属于历史不可变）；
* ``ledger.jsonl``  每次补账一条不可变结果，旧账永远保留；月度结算只取该月
                     ``run_no`` 最大的一条（最新账），历史账可审计回放；
* ``audit.jsonl``   哈希链审计流水：每条带 ``prev_sha256`` 和自身 ``sha256``，
                     任何追加以外的改动都会在 :func:`verify_chain` 断掉。
"""

from __future__ import annotations

import hashlib
import json
import os

from ..errors import StorageError
from ..store.journal import JsonlJournal
from .state import ReconciliationRun, Sample


def _canonical(payload: object) -> bytes:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )


class ReconcileStore:
    """补账结果与审计的落盘封装。"""

    def __init__(self, root: str) -> None:
        self._root = os.path.abspath(root)
        os.makedirs(self._root, exist_ok=True)
        self._raw = JsonlJournal(self._root, "raw", segment_limit=10_000)
        self._ledger = JsonlJournal(self._root, "ledger", segment_limit=10_000)
        self._audit = JsonlJournal(self._root, "audit", segment_limit=10_000)

    @property
    def root(self) -> str:
        return self._root

    @property
    def ledger_journal(self) -> JsonlJournal:
        return self._ledger

    # ---- 原始采样：只归档，永不回写 -------------------------------------

    def append_raw(self, source: str, samples: list[Sample]) -> int:
        """把一批原始采样归档（重复导入会如实留痕，由上层判重）。"""

        for sample in samples:
            self._raw.append({"source": source, "sample": sample.as_dict()})
        return len(samples)

    def read_raw(self) -> list[dict]:
        return self._raw.read_all()

    # ---- 补账总账：只追加，按月取最新 ------------------------------------

    def append_run(self, run: ReconciliationRun) -> dict:
        """追加一条补账结果。返回落盘记录（含 run_no）。"""

        run_no = self._ledger.count()
        record = {"run_no": run_no, "run": run.as_dict()}
        self._ledger.append(record)
        return record

    def list_runs(self, month: str | None = None) -> list[dict]:
        records = self._ledger.read_all()
        if month is not None:
            records = [item for item in records if item["run"]["month"] == month]
        return records

    def latest_run(self, month: str) -> dict | None:
        """返回某月最新一条补账记录；没有则 ``None``。"""

        records = self.list_runs(month)
        return records[-1] if records else None

    # ---- 审计：哈希链 ----------------------------------------------------

    def append_audit(
        self,
        action: str,
        *,
        run_id: str = "",
        month: str = "",
        severity: str = "notice",
        message: str = "",
        **payload: object,
    ) -> dict:
        """追加一条哈希链审计记录。"""

        previous = self._audit.read_all()[-1:]
        prev_sha = previous[0]["sha256"] if previous else None
        body = {
            "action": action,
            "run_id": run_id,
            "month": month,
            "severity": severity,
            "message": message,
            "payload": payload,
            "prev_sha256": prev_sha,
        }
        record = dict(body)
        record["sha256"] = hashlib.sha256(_canonical(body)).hexdigest()
        self._audit.append(record)
        return record

    def audit_entries(self) -> list[dict]:
        return self._audit.read_all()

    def verify_chain(self) -> dict:
        """从头校验审计哈希链与每条自身哈希。返回校验报告。"""

        entries = self._audit.read_all()
        prev_sha = None
        for index, entry in enumerate(entries):
            expected_prev = entries[index - 1]["sha256"] if index > 0 else None
            if entry.get("prev_sha256") != expected_prev:
                return {"ok": False, "broken_at": index, "reason": "prev_sha256_mismatch"}
            stored = entry.pop("sha256")
            recalculated = hashlib.sha256(_canonical(entry)).hexdigest()
            entry["sha256"] = stored
            if recalculated != stored:
                return {"ok": False, "broken_at": index, "reason": "entry_sha256_mismatch"}
            prev_sha = stored
        return {"ok": True, "entries": len(entries), "head_sha256": prev_sha}
