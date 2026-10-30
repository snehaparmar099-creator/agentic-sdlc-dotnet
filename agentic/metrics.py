"""Reliability metrics for a run, derived from engine events.

- success_rate: nodes succeeded / nodes that reached a terminal state
- first_pass_rate: nodes that succeeded on their first attempt / nodes succeeded
- retries, fallbacks, rollbacks, invalidations, policy blocks: counts
- mttr_s: mean time from a node's first failed attempt to its eventual success
- e2e_latency_s: wall time of the run; agent_time_s excludes human approval wait
"""

from __future__ import annotations

import json
import threading
import time
from collections import defaultdict
from pathlib import Path


class Metrics:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.started = time.monotonic()
        self.finished: float | None = None
        self.counters: dict[str, int] = defaultdict(int)
        self.node_attempts: dict[str, int] = defaultdict(int)
        self.node_latency: dict[str, float] = defaultdict(float)
        self.first_failure: dict[str, float] = {}
        self.recoveries: list[float] = []
        self.approval_wait_s = 0.0
        self.terminal: dict[str, str] = {}
        self.llm_tokens = {"input": 0, "output": 0}

    def incr(self, name: str, by: int = 1) -> None:
        with self._lock:
            self.counters[name] += by

    def attempt(self, node_id: str, seconds: float, ok: bool) -> None:
        with self._lock:
            self.node_attempts[node_id] += 1
            self.node_latency[node_id] += seconds
            now = time.monotonic()
            if not ok:
                self.first_failure.setdefault(node_id, now)
                self.counters["failed_attempts"] += 1

    def node_done(self, node_id: str, status: str) -> None:
        with self._lock:
            self.terminal[node_id] = status
            if status == "succeeded" and node_id in self.first_failure:
                self.recoveries.append(time.monotonic() - self.first_failure.pop(node_id))

    def approval_wait(self, seconds: float) -> None:
        with self._lock:
            self.approval_wait_s += seconds

    def tokens(self, inp: int, out: int) -> None:
        with self._lock:
            self.llm_tokens["input"] += inp
            self.llm_tokens["output"] += out

    def finish(self) -> None:
        self.finished = time.monotonic()

    def summary(self) -> dict:
        with self._lock:
            end = self.finished or time.monotonic()
            done = list(self.terminal.values())
            succeeded = [n for n, s in self.terminal.items() if s == "succeeded"]
            first_pass = [n for n in succeeded if self.node_attempts.get(n, 1) <= 1]
            e2e = end - self.started
            return {
                "nodes_total": len(done),
                "nodes_succeeded": len(succeeded),
                "success_rate": round(len(succeeded) / len(done), 3) if done else None,
                "first_pass_rate": round(len(first_pass) / len(succeeded), 3) if succeeded else None,
                "attempts": sum(self.node_attempts.values()),
                "retries": self.counters["retries"],
                "fallbacks": self.counters["fallbacks"],
                "provider_fallbacks": self.counters["provider_fallbacks"],
                "rollbacks": self.counters["rollbacks"],
                "invalidations": self.counters["invalidations"],
                "replans": self.counters["replans"],
                "policy_blocks": self.counters["policy_blocks"],
                "approvals_requested": self.counters["approvals_requested"],
                "approvals_rejected": self.counters["approvals_rejected"],
                "failed_attempts": self.counters["failed_attempts"],
                "mttr_s": round(sum(self.recoveries) / len(self.recoveries), 3) if self.recoveries else None,
                "e2e_latency_s": round(e2e, 3),
                "approval_wait_s": round(self.approval_wait_s, 3),
                "agent_time_s": round(e2e - self.approval_wait_s, 3),
                "node_latency_s": {k: round(v, 3) for k, v in self.node_latency.items()},
                "llm_tokens": dict(self.llm_tokens),
            }


def aggregate(metric_files: list[Path]) -> dict:
    runs = [json.loads(Path(p).read_text(encoding="utf-8")) for p in metric_files]
    if not runs:
        return {"runs": 0}
    total_nodes = sum(r["nodes_total"] for r in runs)
    succeeded = sum(r["nodes_succeeded"] for r in runs)
    mttrs = [r["mttr_s"] for r in runs if r.get("mttr_s") is not None]
    return {
        "runs": len(runs),
        "node_success_rate": round(succeeded / total_nodes, 3) if total_nodes else None,
        "retries_per_run": round(sum(r["retries"] for r in runs) / len(runs), 2),
        "rollbacks_per_run": round(sum(r["rollbacks"] for r in runs) / len(runs), 2),
        "mean_mttr_s": round(sum(mttrs) / len(mttrs), 3) if mttrs else None,
        "mean_e2e_latency_s": round(sum(r["e2e_latency_s"] for r in runs) / len(runs), 3),
        "mean_agent_time_s": round(sum(r["agent_time_s"] for r in runs) / len(runs), 3),
    }
