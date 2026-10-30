"""Persistent run state: node status, cross-stage context, decision lineage.

State is written atomically to runs/<id>/state.json after every transition, so
a run can be inspected at any time and resumed after a safe stop or crash.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .graph import NodeStatus, WorkflowGraph


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def fingerprint(value) -> str:
    payload = json.dumps(value, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


@dataclass
class NodeState:
    status: str = NodeStatus.PENDING.value
    attempts: int = 0
    cycle_attempts: int = 0          # attempts in the current retry cycle (resets on fallback/revision)
    revisions: int = 0              # re-runs requested by a human reviewer
    agent: str | None = None         # agent actually used (primary or fallback)
    used_fallback: bool = False
    output: dict | None = None
    output_fp: str | None = None
    input_fps: dict[str, str] = field(default_factory=dict)
    feedback: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    applied: dict[str, str | None] = field(default_factory=dict)  # path -> content before change
    changed_files: list[str] = field(default_factory=list)
    commit: str | None = None
    started_at: str | None = None
    finished_at: str | None = None
    prev_status: str | None = None   # status before being marked STALE


@dataclass
class Decision:
    id: str
    node: str
    summary: str
    rationale: str
    made_by: str                      # agent name or human approver id
    alternatives: list[str] = field(default_factory=list)
    parents: list[str] = field(default_factory=list)  # decision ids this one builds on
    superseded_by: str | None = None
    ts: str = field(default_factory=now_iso)


class RunState:
    def __init__(self, run_dir: Path, run_id: str, scenario: dict, config: dict) -> None:
        self.run_dir = Path(run_dir)
        self.run_id = run_id
        self.scenario = scenario
        self.config = config
        self.graph = WorkflowGraph()
        self.nodes: dict[str, NodeState] = {}
        self.decisions: list[Decision] = []
        self.findings: list[dict] = []
        self.approvals: list[dict] = []
        self.status = "created"
        self.stop_reason: str | None = None
        self.created_at = now_iso()
        self._lock = threading.RLock()

    # -- nodes -------------------------------------------------------------
    def add_node(self, node) -> None:
        with self._lock:
            self.graph.add(node)
            self.nodes[node.id] = NodeState()

    def remove_node(self, node_id: str) -> None:
        with self._lock:
            self.graph.remove(node_id)
            self.nodes.pop(node_id, None)

    def status_of(self, node_id: str) -> NodeStatus:
        return NodeStatus(self.nodes[node_id].status)

    def set_status(self, node_id: str, status: NodeStatus) -> None:
        with self._lock:
            self.nodes[node_id].status = status.value

    def statuses(self) -> dict[str, str]:
        return {nid: ns.status for nid, ns in self.nodes.items()}

    def outputs_of(self, node_ids) -> dict[str, dict]:
        return {nid: self.nodes[nid].output for nid in node_ids if self.nodes[nid].output is not None}

    def upstream_outputs(self, node_id: str) -> dict[str, dict]:
        """Outputs of every transitive ancestor: the cross-stage context a node sees."""
        anc = self.graph.ancestors(node_id)
        order = [n for n in self.graph.topo_order() if n in anc]
        return self.outputs_of(order)

    # -- decisions ---------------------------------------------------------
    def add_decision(self, node: str, summary: str, rationale: str, made_by: str,
                     alternatives: list[str] | None = None) -> Decision:
        with self._lock:
            parents = [d.id for d in self.decisions
                       if d.node in self.graph.ancestors(node) and d.superseded_by is None] \
                if node in self.graph.nodes else []
            dec = Decision(
                id=f"D{len(self.decisions) + 1:03d}", node=node, summary=summary,
                rationale=rationale, made_by=made_by, alternatives=alternatives or [],
                parents=parents,
            )
            self.decisions.append(dec)
            return dec

    def supersede_decisions(self, node: str, by: str) -> None:
        with self._lock:
            for d in self.decisions:
                if d.node == node and d.superseded_by is None and d.id != by:
                    d.superseded_by = by

    def active_decisions(self) -> list[Decision]:
        return [d for d in self.decisions if d.superseded_by is None]

    # -- persistence -------------------------------------------------------
    def to_dict(self) -> dict:
        with self._lock:
            return {
                "run_id": self.run_id,
                "status": self.status,
                "stop_reason": self.stop_reason,
                "created_at": self.created_at,
                "scenario": self.scenario,
                "config": self.config,
                "graph": self.graph.to_dict(),
                "nodes": {k: asdict(v) for k, v in self.nodes.items()},
                "decisions": [asdict(d) for d in self.decisions],
                "findings": self.findings,
                "approvals": self.approvals,
            }

    def save(self) -> None:
        with self._lock:
            path = self.run_dir / "state.json"
            tmp = path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(self.to_dict(), indent=2, default=str), encoding="utf-8")
            os.replace(tmp, path)

    @classmethod
    def load(cls, run_dir: Path) -> "RunState":
        data = json.loads((Path(run_dir) / "state.json").read_text(encoding="utf-8"))
        st = cls(run_dir, data["run_id"], data["scenario"], data["config"])
        st.status = data["status"]
        st.stop_reason = data.get("stop_reason")
        st.created_at = data["created_at"]
        st.graph = WorkflowGraph.from_dict(data["graph"])
        st.nodes = {k: NodeState(**v) for k, v in data["nodes"].items()}
        st.decisions = [Decision(**d) for d in data["decisions"]]
        st.findings = data["findings"]
        st.approvals = data["approvals"]
        return st
