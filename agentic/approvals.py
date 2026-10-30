"""Human approval checkpoints.

An approval request is raised for (a) stage outputs marked as checkpoints
(design, plan, release), (b) change sets that policy holds for change control,
(c) clarification questions on ambiguous requirements, and (d) nodes that
exhausted retries and fallback.

Approvers
    CliApprover       interactive terminal prompts (default)
    AutoApprover      unattended: approves and picks recommended answers;
                      every decision is recorded as unattended in the audit log
    ScriptedApprover  replays decisions from the scenario file (deterministic demos/CI),
                      deferring to another approver for anything not scripted
"""

from __future__ import annotations

import getpass
import json
import textwrap
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field

APPROVE, REJECT, STOP = "approve", "reject", "stop"


@dataclass
class Question:
    id: str
    text: str
    options: list[str]
    recommended: int = 0
    rationale: str = ""


@dataclass
class ApprovalRequest:
    id: str
    node_id: str
    kind: str                       # stage_output | policy_hold | clarification | failure_escalation
    title: str
    summary: str
    impact: str = "medium"
    details: list[str] = field(default_factory=list)
    questions: list[Question] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ApprovalDecision:
    decision: str                   # approve | reject | stop
    approver: str
    comment: str = ""
    answers: dict[str, str] = field(default_factory=dict)
    amend_node: str | None = None   # re-run this upstream node with the comment as feedback
    unattended: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


class Approver(ABC):
    @abstractmethod
    def decide(self, req: ApprovalRequest) -> ApprovalDecision: ...


class AutoApprover(Approver):
    def __init__(self, label: str = "auto-approve") -> None:
        self.label = label

    def decide(self, req: ApprovalRequest) -> ApprovalDecision:
        if req.kind == "failure_escalation":
            # Unattended runs never push past an unrecovered failure.
            return ApprovalDecision(STOP, self.label, "unattended run: stopping on unrecovered failure",
                                    unattended=True)
        answers = {q.id: q.options[q.recommended] for q in req.questions}
        return ApprovalDecision(APPROVE, self.label, "approved unattended (no scripted decision)",
                                answers=answers, unattended=True)


class CliApprover(Approver):
    def __init__(self, input_fn=input, print_fn=print) -> None:
        self.input, self.print = input_fn, print_fn
        try:
            self.user = getpass.getuser()
        except Exception:
            self.user = "unknown"

    def decide(self, req: ApprovalRequest) -> ApprovalDecision:
        try:
            return self._decide(req)
        except (EOFError, KeyboardInterrupt):
            # No reviewer available (closed stdin / Ctrl-C at the prompt): fail safe.
            return ApprovalDecision(STOP, f"cli:{self.user}", "no reviewer input; stopping safely")

    def _decide(self, req: ApprovalRequest) -> ApprovalDecision:
        p = self.print
        p("\n" + "=" * 78)
        p(f"APPROVAL REQUIRED  [{req.kind}]  node={req.node_id}  impact={req.impact}")
        p(req.title)
        p("-" * 78)
        p(textwrap.indent(req.summary.strip(), "  "))
        for d in req.details[:40]:
            p(f"  - {d}")
        answers: dict[str, str] = {}
        for q in req.questions:
            p(f"\n  Q {q.id}: {q.text}")
            for i, opt in enumerate(q.options, start=1):
                mark = " (recommended)" if i - 1 == q.recommended else ""
                p(f"     {i}. {opt}{mark}")
            raw = self.input(f"  choose 1-{len(q.options)} or type an answer [{q.recommended + 1}]: ").strip()
            if not raw:
                answers[q.id] = q.options[q.recommended]
            elif raw.isdigit() and 1 <= int(raw) <= len(q.options):
                answers[q.id] = q.options[int(raw) - 1]
            else:
                answers[q.id] = raw
        while True:
            choice = self.input("\n  [a]pprove  [r]eject with feedback  [s]top run  > ").strip().lower()
            if choice in ("a", "approve", ""):
                return ApprovalDecision(APPROVE, f"cli:{self.user}", answers=answers)
            if choice in ("r", "reject"):
                comment = self.input("  feedback for the agent: ").strip()
                return ApprovalDecision(REJECT, f"cli:{self.user}", comment=comment, answers=answers)
            if choice in ("s", "stop"):
                return ApprovalDecision(STOP, f"cli:{self.user}", comment="stopped by reviewer")


class ScriptedApprover(Approver):
    """Rules: {"node": "...", "kind": "...", "occurrence": 1, "decision": "...", "comment": "...",
    "answers": {...}, "amend_node": "..."}; node/kind/occurrence are match criteria."""

    def __init__(self, rules: list[dict], fallback: Approver) -> None:
        self.rules = rules
        self.fallback = fallback
        self.seen: dict[tuple[str, str], int] = {}

    def decide(self, req: ApprovalRequest) -> ApprovalDecision:
        key = (req.node_id, req.kind)
        self.seen[key] = self.seen.get(key, 0) + 1
        for rule in self.rules:
            if rule.get("node", req.node_id) != req.node_id:
                continue
            if rule.get("kind", req.kind) != req.kind:
                continue
            if rule.get("occurrence", self.seen[key]) != self.seen[key]:
                continue
            answers = dict(rule.get("answers", {}))
            for q in req.questions:
                answers.setdefault(q.id, q.options[q.recommended])
            return ApprovalDecision(
                decision=rule["decision"],
                approver=f"script:{rule.get('as', 'reviewer')}",
                comment=rule.get("comment", ""),
                answers=answers,
                amend_node=rule.get("amend_node"),
            )
        return self.fallback.decide(req)


def describe(decision: ApprovalDecision) -> str:
    extra = f" answers={json.dumps(decision.answers)}" if decision.answers else ""
    return f"{decision.decision} by {decision.approver}{extra} {decision.comment}".strip()
