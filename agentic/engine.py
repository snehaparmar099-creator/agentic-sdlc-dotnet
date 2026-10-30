"""Orchestration engine.

Control loop (main thread):
    1. schedule every PENDING node whose deps SUCCEEDED and entry gates pass
       (up to max_parallel concurrently, on a worker pool)
    2. wait for any attempt to finish, then handle its outcome:
         success  -> human checkpoint if required -> commit, record context,
                     decision lineage, artifacts, dynamic nodes -> revalidate
                     any STALE dependents (invalidate + roll back if inputs changed)
         hold     -> policy requires change-control approval before applying
         failure  -> bounded retry with backoff -> fallback agent -> human escalation
         fatal    -> safe stop
    3. repeat until nothing is runnable or a safe stop completes

Worker threads only run the agent, evaluate policy, apply changes and run exit
gates for one attempt; every state transition and every human interaction
happens on the main thread, so run state needs no cross-thread coordination
beyond the workspace's own locks.
"""

from __future__ import annotations

import json
import signal
import threading
import time
import traceback
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from pathlib import Path

from .agents import REGISTRY
from .agents.base import AgentContext, AgentError, AgentResult, NodeSpec
from .approvals import APPROVE, REJECT, STOP, ApprovalDecision, ApprovalRequest, Approver, describe
from .audit import AuditLog
from .gates import ENTRY_GATES, EXIT_GATES
from .graph import GraphError, Node, NodeStatus, RetryPolicy
from .llm import BudgetExceeded, LLMError, LLMProvider, OfflineProvider
from .metrics import Metrics
from .playbooks.base import Playbook
from .policy import APPROVAL, BLOCK, Finding, PolicyEngine
from .state import RunState, fingerprint, now_iso
from .workspace import Workspace, WorkspaceError

S = NodeStatus


@dataclass
class EngineConfig:
    max_parallel: int = 4
    max_revisions: int = 3
    rollback_on_stop: bool = False
    verbose: bool = True


@dataclass
class Outcome:
    kind: str                        # success | hold | failure | fatal
    node_id: str
    attempt: int
    duration: float = 0.0
    result: AgentResult | None = None
    findings: list[Finding] = field(default_factory=list)
    gates: dict = field(default_factory=dict)
    snapshot: dict = field(default_factory=dict)
    reason: str = ""
    rolled_back: bool = False


class FaultInjector:
    """Deterministic fault injection for demos and resilience tests.

    spec: {"node": id, "attempts": [1], "type": "transient_error" | "corrupt_output" | "policy_violation",
           "path": ..., "find": ..., "replace": ...}
    """

    def __init__(self, specs: list[dict] | None) -> None:
        self.specs = specs or []

    def _match(self, node_id: str, attempt: int):
        return [s for s in self.specs if s["node"] == node_id and attempt in s.get("attempts", [1])]

    def before(self, node_id: str, attempt: int) -> None:
        for s in self._match(node_id, attempt):
            if s["type"] == "transient_error":
                raise AgentError(f"injected transient fault: {s.get('message', 'provider timeout')}")

    def after(self, node_id: str, attempt: int, result: AgentResult) -> list[str]:
        applied = []
        for s in self._match(node_id, attempt):
            for ch in result.changes:
                if ch.path != s.get("path"):
                    continue
                if s["type"] == "corrupt_output":
                    if ch.content is not None and s["find"] in ch.content:
                        ch.content = ch.content.replace(s["find"], s["replace"], 1)
                        applied.append(f"corrupted {ch.path}")
                    elif ch.replace is not None and s["find"] in ch.replace:
                        ch.replace = ch.replace.replace(s["find"], s["replace"], 1)
                        applied.append(f"corrupted {ch.path}")
                elif s["type"] == "policy_violation" and ch.content is not None:
                    ch.content += "\n\ndef _debug(expr):\n    return eval(expr)\n"
                    applied.append(f"injected eval() into {ch.path}")
        return applied


class Engine:
    def __init__(self, state: RunState, workspace: Workspace, approver: Approver, audit: AuditLog,
                 llm: LLMProvider, policy: PolicyEngine, playbook: Playbook,
                 faults: FaultInjector | None = None, config: EngineConfig | None = None,
                 metrics: Metrics | None = None) -> None:
        self.state = state
        self.workspace = workspace
        self.approver = approver
        self.audit = audit
        self.llm = llm
        self.policy = policy
        self.playbook = playbook
        self.faults = faults or FaultInjector([])
        self.cfg = config or EngineConfig()
        self.metrics = metrics or Metrics()
        self.stopping = False
        self._stop_signal: str | None = None
        self._pool: ThreadPoolExecutor | None = None
        self._inflight: dict[Future, str] = {}
        self._discard: set[str] = set()  # running nodes invalidated mid-flight
        self._approval_seq = len(state.approvals)

    # ------------------------------------------------------------------ util
    def log(self, msg: str) -> None:
        if self.cfg.verbose:
            print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)

    def node(self, nid: str) -> Node:
        return self.state.graph.nodes[nid]

    def ns(self, nid: str):
        return self.state.nodes[nid]

    def _set(self, nid: str, status: S, **audit) -> None:
        self.state.set_status(nid, status)
        self.audit.emit(f"node_{status.value}", node=nid, **audit)

    def request_stop(self, reason: str) -> None:
        self._stop_signal = reason

    # ------------------------------------------------------------------ main
    def run(self) -> str:
        st = self.state
        st.status = "running"
        self.audit.emit("run_started", scenario=st.scenario.get("id"), llm=self.llm.name,
                        approver=type(self.approver).__name__, nodes=list(st.graph.nodes))
        st.save()
        old_handler = None
        if threading.current_thread() is threading.main_thread():
            old_handler = signal.signal(signal.SIGINT, lambda *_: self.request_stop("interrupted (SIGINT)"))
        try:
            with ThreadPoolExecutor(max_workers=self.cfg.max_parallel, thread_name_prefix="agent") as pool:
                self._pool = pool
                self._loop()
        finally:
            if old_handler is not None:
                signal.signal(signal.SIGINT, old_handler)
        return self._finalize()

    def _loop(self) -> None:
        while True:
            reason = self._stop_signal or self._stop_file()
            if reason and not self.stopping:
                self._begin_stop(reason)
            if not self.stopping:
                self._cascade_skips()
                for nid in self._ready():
                    if len(self._inflight) >= self.cfg.max_parallel:
                        break
                    self._start(nid)
            if not self._inflight:
                break
            done, _ = wait(list(self._inflight), timeout=0.25, return_when=FIRST_COMPLETED)
            for fut in done:
                nid = self._inflight.pop(fut)
                try:
                    outcome = fut.result()
                except Exception as exc:  # defensive: worker bugs must not kill the run
                    outcome = Outcome("failure", nid, self.ns(nid).attempts,
                                      reason=f"worker crashed: {exc!r}")
                self._handle(outcome)
                self.state.save()

    def _stop_file(self) -> str | None:
        return "stop requested via STOP file" if (self.state.run_dir / "STOP").exists() else None

    def _ready(self) -> list[str]:
        out = []
        running = set(self._inflight.values())
        for nid in self.state.graph.topo_order():
            if nid in running or self.ns(nid).status != S.PENDING.value:
                continue
            if all(self.ns(d).status == S.SUCCEEDED.value for d in self.node(nid).deps):
                out.append(nid)
        return out

    def _cascade_skips(self) -> None:
        for nid in self.state.graph.topo_order():
            if self.ns(nid).status != S.PENDING.value:
                continue
            bad = [d for d in self.node(nid).deps
                   if self.ns(d).status in (S.FAILED.value, S.SKIPPED.value)]
            if bad:
                self._set(nid, S.SKIPPED, reason=f"upstream {bad} did not succeed")
                self.metrics.node_done(nid, "skipped")
                self.log(f"skip   {nid} (upstream {bad} failed)")

    # -------------------------------------------------------------- schedule
    def _start(self, nid: str, apply_only: AgentResult | None = None) -> None:
        node, ns = self.node(nid), self.ns(nid)
        if apply_only is None:
            for g in node.entry_gates:
                res = ENTRY_GATES[g](self, node)
                self.audit.emit("gate_checked", node=nid, gate=g, kind="entry", **res)
                if not res["passed"]:
                    ns.errors.append(f"entry gate {g}: {res['message']}")
                    self._set(nid, S.FAILED, reason=f"entry gate {g} failed")
                    self.metrics.node_done(nid, "failed")
                    self.log(f"FAIL   {nid}: entry gate {g} - {res['message']}")
                    return
            ns.attempts += 1
            ns.cycle_attempts += 1
        ns.agent = ns.agent or node.agent
        ns.started_at = ns.started_at or now_iso()
        self._set(nid, S.RUNNING, attempt=ns.attempts, agent=ns.agent, apply_only=apply_only is not None)
        tag = " (applying approved change set)" if apply_only else f" attempt {ns.attempts}"
        self.log(f"start  {nid} [{ns.agent}]{tag}")
        fut = self._pool.submit(self._attempt, nid, ns.attempts, ns.cycle_attempts, apply_only)
        self._inflight[fut] = nid

    def _context(self, nid: str, attempt: int) -> AgentContext:
        node, ns = self.node(nid), self.ns(nid)
        force_offline = ns.used_fallback and (node.fallback_agent or "").endswith(":offline")
        facts = {}
        if node.stage == "release":
            facts = {
                "approvals": list(self.state.approvals),
                "node_statuses": self.state.statuses(),
                "changed_files": self.workspace.changed_files("baseline"),
                "findings": list(self.state.findings),
            }
        return AgentContext(
            run_id=self.state.run_id, node=node, scenario=self.state.scenario,
            upstream=self.state.upstream_outputs(nid), workspace=self.workspace,
            llm=OfflineProvider() if force_offline else self.llm, playbook=self.playbook,
            attempt=attempt, feedback=list(ns.feedback), previous_errors=list(ns.errors[-2:]),
            facts=facts,
        )

    # ------------------------------------------------------- worker (thread)
    def _attempt(self, nid: str, attempt: int, cycle_attempt: int,
                 apply_only: AgentResult | None) -> Outcome:
        node, ns = self.node(nid), self.ns(nid)
        t0 = time.monotonic()

        def done(kind: str, **kw) -> Outcome:
            return Outcome(kind, nid, attempt, duration=time.monotonic() - t0, **kw)

        try:
            if apply_only is None:
                delay = node.retry.delay(cycle_attempt)
                if delay:
                    time.sleep(delay)
                agent_name = (ns.agent or node.agent).split(":")[0]
                agent = REGISTRY[agent_name]
                ctx = self._context(nid, attempt)
                self.faults.before(nid, attempt)
                result = agent.run(ctx)
                injected = self.faults.after(nid, attempt, result)
                if injected:
                    result.notes += [f"FAULT INJECTED: {m}" for m in injected]
                findings = self.policy.evaluate(node, result.changes, self.workspace)
                if any(f.severity == BLOCK for f in findings):
                    blocks = [f"{f.rule} {f.path or ''}:{f.line or ''} {f.message}" for f in findings
                              if f.severity == BLOCK]
                    return done("failure", result=result, findings=findings,
                                reason="policy blocked change set: " + "; ".join(blocks))
                if any(f.severity == APPROVAL for f in findings):
                    return done("hold", result=result, findings=findings)
            else:
                result, findings = apply_only, []
            snapshot = self.workspace.apply(result.changes) if result.changes else {}
            gates = {}
            for g in node.exit_gates:
                rep = EXIT_GATES[g](self, node, result)
                gates[g] = rep
                if not rep["passed"]:
                    self.workspace.restore(snapshot)
                    return done("failure", result=result, findings=findings, gates=gates,
                                reason=f"exit gate {g} failed: {rep['message']}", rolled_back=bool(snapshot))
            return done("success", result=result, findings=findings, gates=gates, snapshot=snapshot)
        except BudgetExceeded as exc:
            return done("fatal", reason=str(exc))
        except (AgentError, LLMError, WorkspaceError, GraphError) as exc:
            return done("failure", reason=f"{type(exc).__name__}: {exc}")
        except Exception as exc:
            return done("failure", reason=f"unexpected {type(exc).__name__}: {exc}\n"
                                          f"{traceback.format_exc(limit=3)}")

    # ------------------------------------------------------- outcome handling
    def _handle(self, out: Outcome) -> None:
        nid = out.node_id
        node, ns = self.node(nid), self.ns(nid)
        self.metrics.attempt(nid, out.duration, ok=out.kind in ("success", "hold"))
        if out.result is not None:
            self.metrics.tokens(*out.result.tokens)
            if out.result.provider == "offline-fallback":
                self.metrics.incr("provider_fallbacks")
            self.audit.emit("agent_completed", node=nid, attempt=out.attempt, provider=out.result.provider,
                            summary=out.result.summary, notes=out.result.notes,
                            changes=[c.path for c in out.result.changes], tokens=list(out.result.tokens))
        for f in out.findings:
            rec = {"node": nid, "attempt": out.attempt, **f.to_dict()}
            self.state.findings.append(rec)
            self.audit.emit("policy_finding", **rec)
            if f.severity == BLOCK:
                self.metrics.incr("policy_blocks")
        for g, rep in out.gates.items():
            self.audit.emit("gate_checked", node=nid, gate=g, kind="exit", passed=rep["passed"],
                            message=rep["message"][:500])

        if nid in self._discard:  # invalidated while running: drop the result
            self._discard.discard(nid)
            if out.snapshot:
                self.workspace.restore(out.snapshot)
            self.audit.emit("attempt_discarded", node=nid, reason="upstream changed while running")
            return

        if out.kind == "fatal":
            ns.errors.append(out.reason)
            self._set(nid, S.PENDING, reason=out.reason)
            self._begin_stop(out.reason)
        elif out.kind == "failure":
            self._on_failure(out)
        elif out.kind == "hold":
            self._on_hold(out)
        else:
            self._on_success(out)

    def _on_failure(self, out: Outcome) -> None:
        nid = out.node_id
        node, ns = self.node(nid), self.ns(nid)
        ns.errors.append(out.reason[:4000])
        self.audit.emit("attempt_failed", node=nid, attempt=out.attempt, reason=out.reason[:2000])
        self.log(f"fail   {nid} attempt {out.attempt}: {out.reason.splitlines()[0][:160]}")
        if out.rolled_back:
            self.metrics.incr("rollbacks")
            self.audit.emit("rollback", node=nid, scope="attempt", reason="exit gate failed")
            self.log(f"       rolled back {nid}'s changes")
        if self.stopping:
            self._set(nid, S.PENDING, reason="safe stop in progress")
            return
        if ns.cycle_attempts < node.retry.max_attempts:
            self.metrics.incr("retries")
            self.audit.emit("retry_scheduled", node=nid, next_attempt=ns.attempts + 1,
                            backoff_s=node.retry.delay(ns.cycle_attempts + 1))
            self._set(nid, S.PENDING, reason="retry")
            return
        if node.fallback_agent and not ns.used_fallback:
            ns.used_fallback = True
            ns.agent = node.fallback_agent
            ns.cycle_attempts = 0
            self.metrics.incr("fallbacks")
            self.audit.emit("fallback", node=nid, to_agent=node.fallback_agent)
            self.log(f"       falling back to {node.fallback_agent}")
            self._set(nid, S.PENDING, reason="fallback")
            return
        # Retries and fallback exhausted: a human decides.
        dec = self._ask(ApprovalRequest(
            id=self._next_approval_id(), node_id=nid, kind="failure_escalation", impact=node.impact,
            title=f"{nid} failed after {ns.attempts} attempt(s); retry with guidance, abandon, or stop?",
            summary=out.reason[:1500],
            details=[e.splitlines()[0][:200] for e in ns.errors[-3:]],
        ))
        if dec.decision == APPROVE and ns.revisions < self.cfg.max_revisions:
            ns.revisions += 1
            ns.cycle_attempts = 0
            if dec.comment:
                ns.feedback.append(dec.comment)
            self._set(nid, S.PENDING, reason="human-approved retry")
        elif dec.decision == STOP:
            self._set(nid, S.FAILED, reason="stopped after unrecovered failure")
            self.metrics.node_done(nid, "failed")
            self._begin_stop(f"{nid} failed and reviewer chose to stop")
        else:
            self._set(nid, S.FAILED, reason="abandoned by reviewer")
            self.metrics.node_done(nid, "failed")

    def _on_hold(self, out: Outcome) -> None:
        nid = out.node_id
        node, ns = self.node(nid), self.ns(nid)
        holds = [f for f in out.findings if f.severity == APPROVAL]
        if self.stopping:
            self._set(nid, S.PENDING, reason="safe stop in progress")
            return
        self._set(nid, S.AWAITING_APPROVAL, kind="policy_hold")
        dec = self._ask(ApprovalRequest(
            id=self._next_approval_id(), node_id=nid, kind="policy_hold", impact="high",
            title=f"Change control: {nid} wants to apply changes that need sign-off",
            summary=out.result.summary,
            details=[f"[{f.rule}] {f.path or ''} {f.message}" for f in holds]
                    + [f"change: {c.path}" for c in out.result.changes],
        ))
        if dec.decision == APPROVE:
            self.log(f"       policy hold on {nid} approved; applying")
            self._start(nid, apply_only=out.result)
        elif dec.decision == REJECT:
            self._revise(nid, f"Change-control reviewer rejected: {dec.comment or 'no reason given'}")
        else:
            self._set(nid, S.PENDING, reason="stopped at policy hold")
            self._begin_stop(f"reviewer stopped the run at {nid} policy hold")

    def _on_success(self, out: Outcome) -> None:
        nid = out.node_id
        node, ns = self.node(nid), self.ns(nid)
        result = out.result
        if self.stopping and (node.approval or result.questions):
            if out.snapshot:
                self.workspace.restore(out.snapshot)
            self._set(nid, S.PENDING, reason="safe stop before human checkpoint")
            return

        prior = (ns.output or {}).get("clarifications") or {}
        if result.questions and all(q.id in prior for q in result.questions):
            # Re-run after an amendment: the reviewer already answered these questions.
            result.output["clarifications"] = prior
            self.audit.emit("clarifications_carried_over", node=nid, questions=[q.id for q in result.questions])
            self.log(f"       {nid}: reusing earlier answers to {len(result.questions)} question(s)")
        elif result.questions:
            self._set(nid, S.AWAITING_APPROVAL, kind="clarification")
            dec = self._ask(ApprovalRequest(
                id=self._next_approval_id(), node_id=nid, kind="clarification", impact=node.impact,
                title="The request is ambiguous; choose how to interpret it",
                summary=result.summary,
                details=[a["why"] for a in result.output.get("ambiguities", [])],
                questions=result.questions,
            ))
            if not self._checkpoint_outcome(nid, dec, out):
                return
            result.output["clarifications"] = {
                q.id: {"question": q.text, "answer": dec.answers.get(q.id, q.options[q.recommended]),
                       "answered_by": dec.approver} for q in result.questions}
        elif node.approval:
            self._set(nid, S.AWAITING_APPROVAL, kind="stage_output")
            dec = self._ask(ApprovalRequest(
                id=self._next_approval_id(), node_id=nid, kind="stage_output", impact=node.impact,
                title=f"Review {node.stage} output from {nid}",
                summary=result.summary,
                details=self._review_details(result),
            ))
            if not self._checkpoint_outcome(nid, dec, out):
                return
        self._complete(out)

    def _checkpoint_outcome(self, nid: str, dec: ApprovalDecision, out: Outcome) -> bool:
        """Apply a reviewer's decision at a checkpoint; True means proceed to completion."""
        if dec.decision == APPROVE:
            return True
        if out.snapshot:
            self.workspace.restore(out.snapshot)
        if dec.decision == STOP:
            self._set(nid, S.PENDING, reason="reviewer stopped run")
            self._begin_stop(f"reviewer stopped the run at {nid}")
            return False
        if dec.amend_node and dec.amend_node != nid:
            self._set(nid, S.PENDING, reason=f"upstream {dec.amend_node} amended by reviewer")
            self._reset_for_rerun(dec.amend_node, f"Reviewer amendment: {dec.comment}")
        else:
            self._revise(nid, dec.comment or "reviewer rejected without comment")
        return False

    def _revise(self, nid: str, feedback: str) -> None:
        ns = self.ns(nid)
        if ns.revisions >= self.cfg.max_revisions:
            self._set(nid, S.FAILED, reason=f"revision limit {self.cfg.max_revisions} reached")
            self.metrics.node_done(nid, "failed")
            self.log(f"FAIL   {nid}: revision limit reached")
            return
        ns.revisions += 1
        ns.cycle_attempts = 0
        ns.feedback.append(feedback)
        self.audit.emit("revision_requested", node=nid, revision=ns.revisions, feedback=feedback)
        self.log(f"revise {nid} (revision {ns.revisions}): {feedback[:120]}")
        self._set(nid, S.PENDING, reason="revision requested")

    def _review_details(self, result: AgentResult) -> list[str]:
        out = result.output
        details = [f"decision: {d['summary']}" for d in result.decisions]
        if "tasks" in out:
            details += [f"task {t['id']}: {t['title']} (deps={t['deps']}, files={t['files']})" for t in out["tasks"]]
        if "checklist" in out:
            details += [f"[{'x' if c['ok'] else ' '}] {c['item']}" for c in out["checklist"]]
        if "components" in out:
            details += [f"component {c['name']}: {c['responsibility']}" for c in out["components"]]
        details += [f"change: {c.path}" for c in result.changes]
        return details

    # ------------------------------------------------------------- completion
    def _complete(self, out: Outcome) -> None:
        nid, result = out.node_id, out.result
        node, ns = self.node(nid), self.ns(nid)
        new_fp = fingerprint(result.output)
        old_fp = ns.output_fp
        output = dict(result.output)
        output["gates"] = out.gates
        ns.output, ns.output_fp = output, new_fp
        ns.input_fps = {d: self.ns(d).output_fp for d in node.deps}
        ns.applied = out.snapshot
        ns.changed_files = sorted(out.snapshot)
        ns.commit = self.workspace.commit(f"[{nid}] {result.summary}", ns.changed_files) or ns.commit
        ns.finished_at = now_iso()

        prior = [d for d in self.state.decisions if d.node == nid and d.superseded_by is None]
        made_by = f"{ns.agent}@{result.provider}"
        first = None
        for d in result.decisions:
            dec = self.state.add_decision(nid, d["summary"], d.get("rationale", ""), made_by,
                                          d.get("alternatives"))
            first = first or dec
            self.audit.emit("decision_recorded", node=nid, id=dec.id, summary=dec.summary, parents=dec.parents)
        if first and prior:
            for d in prior:
                if not d.made_by.startswith(("cli:", "script:", "auto-approve")):
                    d.superseded_by = first.id
        for name, content in result.artifacts.items():
            path = self.state.run_dir / "artifacts" / name
            path.parent.mkdir(exist_ok=True)
            path.write_text(content, encoding="utf-8")
            self.audit.emit("artifact_written", node=nid, path=str(path.relative_to(self.state.run_dir)))

        changed = old_fp is not None and old_fp != new_fp
        self._set(nid, S.SUCCEEDED, commit=ns.commit, output_fp=new_fp, files=ns.changed_files,
                  output_changed=changed)
        self.metrics.node_done(nid, "succeeded")
        self.log(f"done   {nid}: {result.summary}" + (f" [commit {ns.commit[:8]}]" if ns.commit else ""))
        self._resolve_stale(nid)
        if result.new_nodes or any(n.created_by == nid for n in self.state.graph.nodes.values()):
            self._expand(nid, result.new_nodes)

    def _expand(self, nid: str, specs: list[NodeSpec]) -> None:
        """Insert planner-produced nodes; replaces any previous expansion by the same node."""
        graph = self.state.graph
        existing = [n for n in graph.nodes.values() if n.created_by == nid]
        if existing and all(self.ns(n.id).status == S.SUCCEEDED.value for n in existing) \
                and {n.id for n in existing} == {s.id for s in specs}:
            return  # unchanged plan, already-executed tasks stay valid
        for n in existing:
            self.state.remove_node(n.id)
        joins = [n.id for n in graph.nodes.values() if n.params.get("join_dynamic_from") == nid]
        added = []
        for s in specs:
            self.state.add_node(Node(
                id=s.id, stage=s.stage, agent=s.agent, deps=list(s.deps), params=s.params,
                entry_gates=s.entry_gates, exit_gates=s.exit_gates, impact=s.impact,
                write_scope=s.write_scope, dynamic=True, created_by=nid,
                fallback_agent=s.fallback_agent or f"{s.agent}:offline",
                retry=RetryPolicy(max_attempts=3),
            ))
            added.append(s.id)
            for j in joins:
                graph.add_dep(j, s.id)
        try:
            graph.validate()
        except GraphError as exc:
            for a in added:
                self.state.remove_node(a)
            self.ns(nid).errors.append(str(exc))
            self._set(nid, S.FAILED, reason=f"invalid expansion: {exc}")
            self.metrics.node_done(nid, "failed")
            self.log(f"FAIL   {nid}: planner produced an invalid graph: {exc}")
            return
        kind = "replan" if existing else "expand"
        if existing:
            self.metrics.incr("replans")
        self.audit.emit(f"graph_{kind}", node=nid, removed=[n.id for n in existing], added=added,
                        joins=joins, mermaid=graph.to_mermaid())
        self.log(f"{kind:6} graph: +{len(added)} node(s) from {nid}" +
                 (f", replaced {len(existing)}" if existing else ""))

    # ---------------------------------------------------- invalidation/replan
    def _reset_for_rerun(self, target: str, feedback: str) -> None:
        """Re-run an upstream node; everything downstream becomes STALE until it settles."""
        ns = self.ns(target)
        if ns.applied:
            self.workspace.restore(ns.applied)
            self.workspace.commit(f"[rollback] {target} for re-run", list(ns.applied))
            ns.applied = {}
        ns.feedback.append(feedback)
        ns.revisions += 1
        ns.cycle_attempts = 0
        self.audit.emit("rerun_requested", node=target, feedback=feedback)
        self.log(f"rerun  {target}: {feedback[:120]}")
        self._set(target, S.PENDING, reason="upstream amendment")
        running = set(self._inflight.values())
        for d in self.state.graph.descendants(target):
            dns = self.ns(d)
            if dns.status == S.STALE.value:
                continue
            if d in running:
                self._discard.add(d)
                dns.prev_status = S.PENDING.value
            else:
                dns.prev_status = dns.status if dns.status == S.SUCCEEDED.value else S.PENDING.value
            dns.status = S.STALE.value
            self.audit.emit("node_stale", node=d, upstream=target)

    def _resolve_stale(self, nid: str) -> None:
        """After nid succeeds, settle STALE descendants: keep if inputs unchanged, else invalidate."""
        order = self.state.graph.descendants(nid)
        to_rollback = []
        for d in order:
            dns = self.ns(d)
            if dns.status != S.STALE.value:
                continue
            deps = self.node(d).deps
            if any(self.ns(x).status == S.STALE.value and x not in order for x in deps):
                continue  # waits on another branch
            inputs_same = all(self.ns(x).status == S.SUCCEEDED.value and
                              self.ns(x).output_fp == dns.input_fps.get(x) for x in deps)
            if dns.prev_status == S.SUCCEEDED.value and inputs_same:
                dns.status = S.SUCCEEDED.value
                self.audit.emit("node_revalidated", node=d, reason="inputs unchanged")
                continue
            if dns.prev_status == S.SUCCEEDED.value:
                to_rollback.append(d)
                self.metrics.incr("invalidations")
                self.audit.emit("node_invalidated", node=d, reason=f"upstream output changed ({nid})")
                self.log(f"invalidate {d}: upstream output changed")
            dns.status = S.PENDING.value
            dns.cycle_attempts = 0
            dns.prev_status = None
        for d in reversed(to_rollback):  # undo downstream first
            dns = self.ns(d)
            if dns.applied:
                self.workspace.restore(dns.applied)
                self.workspace.commit(f"[rollback] {d} invalidated", list(dns.applied))
                self.metrics.incr("rollbacks")
                self.audit.emit("rollback", node=d, scope="invalidation", files=list(dns.applied))
                dns.applied = {}
            dns.output = None

    # ------------------------------------------------------------- approvals
    def _next_approval_id(self) -> str:
        self._approval_seq += 1
        return f"A{self._approval_seq:03d}"

    def _ask(self, req: ApprovalRequest) -> ApprovalDecision:
        self.metrics.incr("approvals_requested")
        self.audit.emit("approval_requested", **req.to_dict())
        t0 = time.monotonic()
        dec = self.approver.decide(req)
        self.metrics.approval_wait(time.monotonic() - t0)
        if dec.decision != APPROVE:
            self.metrics.incr("approvals_rejected")
        self.state.approvals.append({"request": req.to_dict(), "decision": dec.to_dict(), "ts": now_iso()})
        self.audit.emit("approval_decided", id=req.id, node=req.node_id, kind=req.kind, **dec.to_dict())
        self.state.add_decision(req.node_id, f"Human {dec.decision} at {req.kind} checkpoint"
                                + (f": {dec.comment}" if dec.comment else ""),
                                rationale=json.dumps(dec.answers) if dec.answers else dec.comment,
                                made_by=dec.approver)
        self.log(f"human  {req.node_id} [{req.kind}]: {describe(dec)}")
        self.state.save()
        return dec

    # ------------------------------------------------------------- stopping
    def _begin_stop(self, reason: str) -> None:
        if self.stopping:
            return
        self.stopping = True
        self.state.stop_reason = reason
        self.audit.emit("safe_stop", reason=reason, inflight=list(self._inflight.values()))
        self.log(f"SAFE STOP: {reason} (waiting for {len(self._inflight)} in-flight node(s))")

    def _finalize(self) -> str:
        st = self.state
        statuses = st.statuses()
        if self.stopping:
            st.status = "stopped"
            if self.cfg.rollback_on_stop:
                rollback_run(self, "baseline", reason=f"rollback on stop: {st.stop_reason}")
        elif all(s == S.SUCCEEDED.value for s in statuses.values()):
            st.status = "succeeded"
        else:
            st.status = "failed"
        self.metrics.finish()
        summary = self.metrics.summary()
        (st.run_dir / "metrics.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
        self.audit.emit("run_finished", status=st.status, stop_reason=st.stop_reason, metrics=summary)
        st.save()
        return st.status


def rollback_run(engine_or_state, ref: str, reason: str = "manual rollback") -> list[str]:
    """Reset the workspace to `ref` (a tag such as 'baseline' or a node id's commit)."""
    if isinstance(engine_or_state, Engine):
        st, ws, audit, metrics = (engine_or_state.state, engine_or_state.workspace,
                                  engine_or_state.audit, engine_or_state.metrics)
    else:
        st, ws, audit, metrics = engine_or_state
    target = ref
    if ref in st.nodes:
        target = st.nodes[ref].commit
        if not target:
            raise WorkspaceError(f"node {ref} has no commit to roll back to")
    target = ws.rev_parse(target)
    # Nodes whose commits are not in the target's history lose their changes...
    lost = {nid for nid, ns in st.nodes.items()
            if ns.status == S.SUCCEEDED.value and ns.commit and not ws.is_ancestor(ns.commit, target)}
    # ...and everything downstream of them was validated against code that no longer exists.
    for nid in list(lost):
        lost.update(d for d in st.graph.descendants(nid) if st.nodes[d].status == S.SUCCEEDED.value)
    ws.reset_hard(target)
    rolled = [nid for nid in st.graph.topo_order() if nid in lost]
    for nid in rolled:
        st.nodes[nid].status = S.ROLLED_BACK.value
    st.status = "rolled_back" if rolled else st.status
    if metrics:
        metrics.incr("rollbacks")
    audit.emit("rollback", scope="run", to=ref, commit=target, nodes=rolled, reason=reason)
    st.save()
    return rolled
