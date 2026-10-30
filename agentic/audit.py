"""Append-only, hash-chained audit log (JSON Lines).

Every event carries the SHA-256 of the previous event, so any edit, deletion
or reordering of past events is detectable with `verify()`.
"""

from __future__ import annotations

import hashlib
import json
import threading
from datetime import datetime, timezone
from pathlib import Path

GENESIS = "0" * 64


def _digest(record: dict) -> str:
    payload = json.dumps(record, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode()).hexdigest()


class AuditLog:
    def __init__(self, path: Path, run_id: str) -> None:
        self.path = Path(path)
        self.run_id = run_id
        self._lock = threading.Lock()
        self._seq = 0
        self._prev = GENESIS
        if self.path.exists():
            for line in self.path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    rec = json.loads(line)
                    self._seq, self._prev = rec["seq"], rec["hash"]

    def emit(self, event: str, **data) -> dict:
        with self._lock:
            self._seq += 1
            record = {
                "seq": self._seq,
                "ts": datetime.now(timezone.utc).isoformat(),
                "run_id": self.run_id,
                "event": event,
                "data": data,
                "prev_hash": self._prev,
            }
            record["hash"] = _digest(record)
            self._prev = record["hash"]
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, default=str) + "\n")
            return record

    def events(self) -> list[dict]:
        return read_events(self.path)


def read_events(path: Path) -> list[dict]:
    path = Path(path)
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def verify(path: Path) -> tuple[bool, str]:
    prev = GENESIS
    for i, rec in enumerate(read_events(path), start=1):
        claimed = rec.get("hash")
        body = {k: v for k, v in rec.items() if k != "hash"}
        if rec.get("seq") != i:
            return False, f"sequence gap at event {i} (found seq {rec.get('seq')})"
        if rec.get("prev_hash") != prev:
            return False, f"chain broken at seq {i}: prev_hash mismatch"
        if _digest(body) != claimed:
            return False, f"event seq {i} was modified (hash mismatch)"
        prev = claimed
    return True, "audit chain intact"
