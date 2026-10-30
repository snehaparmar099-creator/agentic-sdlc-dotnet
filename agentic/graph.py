"""Workflow dependency graph.

Nodes are SDLC stages (requirements, design, ...) or dynamic implementation
tasks inserted by the planner at runtime. Edges are explicit dependencies; a
node becomes runnable only when every dependency has SUCCEEDED and its entry
gates pass. Nodes with no path between them may run in parallel; a node with
several dependencies is a synchronization point (join).
"""

from __future__ import annotations

import fnmatch
from dataclasses import asdict, dataclass, field
from enum import Enum


class NodeStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    AWAITING_APPROVAL = "awaiting_approval"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED = "skipped"          # an upstream node failed
    STALE = "stale"              # an upstream node is re-running; may be invalidated
    ROLLED_BACK = "rolled_back"  # changes reverted by an explicit rollback


TERMINAL = {NodeStatus.SUCCEEDED, NodeStatus.FAILED, NodeStatus.SKIPPED, NodeStatus.ROLLED_BACK}


@dataclass
class RetryPolicy:
    max_attempts: int = 3
    backoff_base_s: float = 0.05
    backoff_max_s: float = 1.0

    def delay(self, attempt: int) -> float:
        """Exponential backoff before attempt N (attempt 1 has no delay)."""
        if attempt <= 1:
            return 0.0
        return min(self.backoff_max_s, self.backoff_base_s * (2 ** (attempt - 2)))


@dataclass
class Node:
    id: str
    stage: str
    agent: str
    deps: list[str] = field(default_factory=list)
    params: dict = field(default_factory=dict)
    entry_gates: list[str] = field(default_factory=list)
    exit_gates: list[str] = field(default_factory=list)
    approval: bool = False           # human must approve output before it is committed
    impact: str = "low"              # low | medium | high
    retry: RetryPolicy = field(default_factory=RetryPolicy)
    fallback_agent: str | None = None
    write_scope: list[str] = field(default_factory=list)  # glob patterns; empty = no writes
    dynamic: bool = False            # created at runtime by the planner
    created_by: str | None = None

    def may_write(self, path: str) -> bool:
        return any(fnmatch.fnmatch(path, pat) for pat in self.write_scope)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Node":
        d = dict(d)
        d["retry"] = RetryPolicy(**d.get("retry", {}))
        return cls(**d)


class GraphError(Exception):
    pass


class WorkflowGraph:
    def __init__(self, nodes: list[Node] | None = None) -> None:
        self.nodes: dict[str, Node] = {}
        for n in nodes or []:
            self.add(n)

    # -- structure ---------------------------------------------------------
    def add(self, node: Node) -> None:
        if node.id in self.nodes:
            raise GraphError(f"duplicate node id: {node.id}")
        self.nodes[node.id] = node

    def remove(self, node_id: str) -> None:
        self.nodes.pop(node_id)
        for n in self.nodes.values():
            if node_id in n.deps:
                n.deps.remove(node_id)

    def add_dep(self, node_id: str, dep: str) -> None:
        if dep not in self.nodes[node_id].deps:
            self.nodes[node_id].deps.append(dep)

    def dependents(self, node_id: str) -> list[str]:
        return [n.id for n in self.nodes.values() if node_id in n.deps]

    def descendants(self, node_id: str) -> list[str]:
        """All transitive dependents, in topological order."""
        seen: set[str] = set()
        stack = [node_id]
        while stack:
            for d in self.dependents(stack.pop()):
                if d not in seen:
                    seen.add(d)
                    stack.append(d)
        return [n for n in self.topo_order() if n in seen]

    def ancestors(self, node_id: str) -> set[str]:
        seen: set[str] = set()
        stack = list(self.nodes[node_id].deps)
        while stack:
            d = stack.pop()
            if d not in seen:
                seen.add(d)
                stack.extend(self.nodes[d].deps)
        return seen

    # -- validation --------------------------------------------------------
    def topo_order(self) -> list[str]:
        indeg = {nid: 0 for nid in self.nodes}
        for n in self.nodes.values():
            for d in n.deps:
                if d not in self.nodes:
                    raise GraphError(f"node {n.id} depends on unknown node {d}")
                indeg[n.id] += 1
        # Stable ordering: preserve insertion order among ready nodes.
        order: list[str] = []
        ready = [nid for nid in self.nodes if indeg[nid] == 0]
        while ready:
            nid = ready.pop(0)
            order.append(nid)
            for dep in self.dependents(nid):
                indeg[dep] -= 1
                if indeg[dep] == 0:
                    ready.append(dep)
        if len(order) != len(self.nodes):
            cyclic = sorted(set(self.nodes) - set(order))
            raise GraphError(f"dependency cycle among: {', '.join(cyclic)}")
        return order

    def parallel_scope_conflicts(self) -> list[tuple[str, str, str]]:
        """Pairs of nodes that may run concurrently and declare overlapping write scopes.

        Two nodes may run concurrently when neither is an ancestor of the other.
        Overlap is checked pattern-against-pattern, which is conservative.
        """
        conflicts = []
        ids = [n for n in self.topo_order() if self.nodes[n].write_scope]
        anc = {n: self.ancestors(n) for n in ids}
        for i, a in enumerate(ids):
            for b in ids[i + 1:]:
                if a in anc[b] or b in anc[a]:
                    continue
                for pa in self.nodes[a].write_scope:
                    for pb in self.nodes[b].write_scope:
                        if fnmatch.fnmatch(pa, pb) or fnmatch.fnmatch(pb, pa):
                            conflicts.append((a, b, pa if pa == pb else f"{pa} ~ {pb}"))
        return conflicts

    def validate(self) -> None:
        self.topo_order()
        conflicts = self.parallel_scope_conflicts()
        if conflicts:
            desc = "; ".join(f"{a} & {b} on {p}" for a, b, p in conflicts)
            raise GraphError(f"parallel nodes with overlapping write scopes: {desc}")

    # -- serialization -----------------------------------------------------
    def to_dict(self) -> list[dict]:
        return [n.to_dict() for n in self.nodes.values()]

    @classmethod
    def from_dict(cls, data: list[dict]) -> "WorkflowGraph":
        return cls([Node.from_dict(d) for d in data])

    def to_mermaid(self, statuses: dict[str, str] | None = None) -> str:
        statuses = statuses or {}
        style = {
            "succeeded": "fill:#d4edda,stroke:#28a745",
            "failed": "fill:#f8d7da,stroke:#dc3545",
            "skipped": "fill:#eee,stroke:#999",
            "rolled_back": "fill:#fff3cd,stroke:#ffc107",
            "pending": "fill:#fff,stroke:#999",
        }
        lines = ["flowchart TD"]
        for nid in self.topo_order():
            n = self.nodes[nid]
            label = f"{nid}<br/><i>{n.agent}</i>"
            if n.approval:
                label += "<br/>🔒 approval"
            safe = nid.replace(".", "_")
            lines.append(f'    {safe}["{label}"]')
            for d in n.deps:
                lines.append(f"    {d.replace('.', '_')} --> {safe}")
        for nid, st in statuses.items():
            if nid in self.nodes and st in style:
                lines.append(f"    style {nid.replace('.', '_')} {style[st]}")
        return "\n".join(lines)
