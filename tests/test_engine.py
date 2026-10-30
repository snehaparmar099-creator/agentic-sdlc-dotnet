"""Engine behaviour on small synthetic workflows with scripted fake agents."""

import threading
import time

import pytest

from agentic.agents import REGISTRY
from agentic.agents.base import Agent, AgentError, AgentResult, NodeSpec
from agentic.approvals import APPROVE, AutoApprover, ScriptedApprover
from agentic.audit import AuditLog, verify
from agentic.engine import Engine, EngineConfig, FaultInjector
from agentic.graph import Node, RetryPolicy
from agentic.llm import Budget, BudgetExceeded, OfflineProvider
from agentic.playbooks.base import Playbook
from agentic.policy import PolicyEngine
from agentic.state import RunState
from agentic.workspace import Change, Workspace


class Fake(Agent):
    """Behaviour driven by node.params:
    fail_attempts: attempts that raise AgentError     files: {path: content} to write
    sleep: seconds                                    output: dict (may depend on feedback)
    """
    name = "fake"
    calls: list = []
    lock = threading.Lock()

    def run_offline(self, ctx):
        p = ctx.node.params
        with self.lock:
            self.calls.append((ctx.node.id, ctx.attempt, time.monotonic()))
        if p.get("sleep"):
            time.sleep(p["sleep"])
        if ctx.attempt in p.get("fail_attempts", []):
            raise AgentError("scripted failure")
        files = p.get("files_by_feedback", {}).get(str(bool(ctx.feedback)), p.get("files", {}))
        out = dict(p.get("output", {"node": ctx.node.id}))
        if ctx.feedback:
            out["feedback"] = list(ctx.feedback)
        out.update(p.get("output_by_feedback", {}).get(str(bool(ctx.feedback)), {}))
        specs = [NodeSpec(**s) for s in p.get("expand", [])]
        return AgentResult(output=out, summary=f"{ctx.node.id} ok",
                           changes=[Change(k, v) for k, v in files.items()],
                           decisions=[{"summary": f"{ctx.node.id} decided", "rationale": "test"}],
                           new_nodes=specs)


@pytest.fixture(autouse=True)
def fake_agent(monkeypatch):
    Fake.calls = []
    monkeypatch.setitem(REGISTRY, "fake", Fake())


def make_engine(tmp_path, nodes, approver=None, faults=None, max_parallel=4, llm=None, policy=None):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    st = RunState(run_dir, "t", {"id": "t", "requirement": "r"}, {})
    for n in nodes:
        st.add_node(n)
    st.graph.validate()
    ws = Workspace(run_dir / "workspace")
    ws.init(None)
    return Engine(st, ws, approver or AutoApprover(), AuditLog(run_dir / "audit.jsonl", "t"),
                  llm or OfflineProvider(), policy or PolicyEngine(), Playbook(), FaultInjector(faults),
                  EngineConfig(max_parallel=max_parallel, verbose=False))


def N(nid, deps=(), **kw):
    params = kw.pop("params", {})
    return Node(nid, kw.pop("stage", "s"), "fake", deps=list(deps), params=params,
                retry=kw.pop("retry", RetryPolicy(max_attempts=3, backoff_base_s=0.0)), **kw)


def status(e):
    return e.state.statuses()


def test_parallel_branches_and_join(tmp_path):
    e = make_engine(tmp_path, [N("a"), N("b", ["a"], params={"sleep": 0.3}), N("c", ["a"], params={"sleep": 0.3}),
                               N("d", ["b", "c"])])
    t0 = time.monotonic()
    assert e.run() == "succeeded"
    assert time.monotonic() - t0 < 0.55  # b and c overlapped
    starts = {nid: t for nid, _, t in Fake.calls}
    assert starts["d"] >= max(starts["b"], starts["c"]) + 0.29  # join waited for both


def test_retry_then_success_records_mttr(tmp_path):
    e = make_engine(tmp_path, [N("a", params={"fail_attempts": [1, 2]})])
    assert e.run() == "succeeded"
    m = e.metrics.summary()
    assert m["retries"] == 2 and m["mttr_s"] is not None and e.state.nodes["a"].attempts == 3


def test_fallback_after_retries_exhausted(tmp_path):
    n = N("a", params={"fail_attempts": [1, 2, 3]}, fallback_agent="fake:offline")
    e = make_engine(tmp_path, [n])
    assert e.run() == "succeeded"
    assert e.state.nodes["a"].used_fallback and e.metrics.summary()["fallbacks"] == 1


def test_unrecovered_failure_safe_stops_unattended_and_skips_nothing_else(tmp_path):
    e = make_engine(tmp_path, [N("a", params={"fail_attempts": [1, 2, 3]}), N("b", ["a"]), N("x")])
    assert e.run() == "stopped"
    assert status(e)["a"] == "failed"
    assert status(e)["b"] == "pending"  # never started after the stop
    assert "a failed" in e.state.stop_reason


def test_abandoned_failure_skips_downstream(tmp_path):
    approver = ScriptedApprover([{"node": "a", "kind": "failure_escalation", "decision": "reject"}], AutoApprover())
    e = make_engine(tmp_path, [N("a", params={"fail_attempts": [1, 2, 3]}), N("b", ["a"]), N("x")], approver)
    assert e.run() == "failed"
    assert status(e) == {"a": "failed", "b": "skipped", "x": "succeeded"}


def test_exit_gate_failure_rolls_back_changes(tmp_path):
    n = Node("impl", "implementation", "fake", write_scope=["*"], exit_gates=["compiles"],
             retry=RetryPolicy(max_attempts=2, backoff_base_s=0),
             params={"files": {"ok.py": "x = 1\n"}})
    e = make_engine(tmp_path, [n], faults=[{"node": "impl", "attempts": [1], "type": "corrupt_output",
                                            "path": "ok.py", "find": "x = 1", "replace": "x = = 1"}])
    # Corrupted syntax is blocked by policy before it ever touches the workspace.
    assert e.run() == "succeeded"
    assert e.workspace.read("ok.py") == "x = 1\n"
    assert any(f["rule"] == "quality.syntax_error" for f in e.state.findings)


def test_failing_exit_gate_restores_workspace(tmp_path):
    files = {"tests/test_x.py": "def test_x():\n    assert False\n"}
    n = Node("impl", "implementation", "fake", write_scope=["*"], exit_gates=["task_tests"],
             retry=RetryPolicy(max_attempts=1), params={"files": files, "task": {"tests": ["tests/test_x.py"]}})
    approver = ScriptedApprover([{"node": "impl", "kind": "failure_escalation", "decision": "reject"}], AutoApprover())
    e = make_engine(tmp_path, [n], approver)
    assert e.run() == "failed"
    assert not e.workspace.exists("tests/test_x.py")
    assert e.metrics.summary()["rollbacks"] == 1


def test_reviewer_rejection_revises_with_feedback(tmp_path):
    approver = ScriptedApprover([{"node": "a", "occurrence": 1, "decision": "reject", "comment": "more detail"}],
                                AutoApprover())
    e = make_engine(tmp_path, [N("a", approval=True)], approver)
    assert e.run() == "succeeded"
    assert e.state.nodes["a"].output["feedback"] == ["more detail"]
    assert e.state.nodes["a"].revisions == 1


def test_revision_limit_is_enforced(tmp_path):
    approver = ScriptedApprover([{"node": "a", "decision": "reject", "comment": "no"}], AutoApprover())
    e = make_engine(tmp_path, [N("a", approval=True)], approver)
    assert e.run() == "failed"
    assert e.state.nodes["a"].revisions == e.cfg.max_revisions


def test_policy_hold_requires_approval_then_applies(tmp_path):
    n = Node("impl", "implementation", "fake", write_scope=["*"],
             params={"files": {"requirements.txt": "redis\n"}})
    approver = ScriptedApprover([{"node": "impl", "kind": "policy_hold", "decision": "approve"}], AutoApprover())
    e = make_engine(tmp_path, [n], approver)
    assert e.run() == "succeeded"
    assert e.workspace.read("requirements.txt") == "redis\n"
    assert [a["request"]["kind"] for a in e.state.approvals] == ["policy_hold"]


def test_policy_hold_rejected_revises_without_dependency(tmp_path):
    n = Node("impl", "implementation", "fake", write_scope=["*"],
             params={"files_by_feedback": {"False": {"requirements.txt": "redis\n"}, "True": {"lim.py": "x = 1\n"}}})
    approver = ScriptedApprover([{"node": "impl", "kind": "policy_hold", "decision": "reject", "comment": "no infra"}],
                                AutoApprover())
    e = make_engine(tmp_path, [n], approver)
    assert e.run() == "succeeded"
    assert not e.workspace.exists("requirements.txt") and e.workspace.exists("lim.py")


def test_policy_block_is_never_applied(tmp_path):
    n = Node("impl", "implementation", "fake", write_scope=["*"], retry=RetryPolicy(max_attempts=2),
             params={"files": {"bad.py": "eval(input())\n"}})
    approver = ScriptedApprover([{"node": "impl", "kind": "failure_escalation", "decision": "reject"}], AutoApprover())
    e = make_engine(tmp_path, [n], approver)
    assert e.run() == "failed"
    assert not e.workspace.exists("bad.py")
    assert e.metrics.summary()["policy_blocks"] == 2


def test_amendment_invalidates_changed_downstream_and_rolls_back(tmp_path):
    nodes = [
        N("req", params={"output_by_feedback": {"True": {"scope": "bigger"}}}),
        N("design", ["req"]),
        N("side", ["req"], params={"output": {"stable": True}}),  # output ignores upstream
        Node("impl", "implementation", "fake", deps=["design"], write_scope=["*"],
             params={"files": {"impl.py": "v = 1\n"}}),
        N("review", ["impl", "side"], approval=True),
    ]
    approver = ScriptedApprover([{"node": "review", "occurrence": 1, "decision": "reject", "amend_node": "req",
                                  "comment": "scope changed"}], AutoApprover())
    e = make_engine(tmp_path, nodes, approver)
    assert e.run() == "succeeded"
    events = [ev["event"] for ev in e.audit.events()]
    invalidated = {ev["data"]["node"] for ev in e.audit.events() if ev["event"] == "node_invalidated"}
    assert {"design", "side", "impl"} <= invalidated
    assert "rerun_requested" in events and e.metrics.summary()["invalidations"] >= 3
    assert e.state.nodes["req"].output["scope"] == "bigger"
    assert e.workspace.read("impl.py") == "v = 1\n"
    assert verify(e.state.run_dir / "audit.jsonl")[0]


def test_dynamic_expansion_wires_join(tmp_path):
    spec = {"id": "impl.x", "stage": "implementation", "agent": "fake", "deps": ["plan"], "write_scope": ["x.py"],
            "params": {"files": {"x.py": "x = 1\n"}}}
    spec2 = dict(spec, id="impl.y", write_scope=["y.py"], params={"files": {"y.py": "y = 1\n"}})
    nodes = [N("plan", params={"expand": [spec, spec2]}),
             Node("test", "testing", "fake", deps=["plan"], params={"join_dynamic_from": "plan"})]
    e = make_engine(tmp_path, nodes)
    assert e.run() == "succeeded"
    assert set(e.state.graph.nodes["test"].deps) == {"plan", "impl.x", "impl.y"}
    order = [c[0] for c in Fake.calls]
    assert order.index("test") > max(order.index("impl.x"), order.index("impl.y"))


def test_invalid_expansion_with_overlapping_scopes_fails_planner(tmp_path):
    spec = {"id": "impl.x", "stage": "implementation", "agent": "fake", "deps": ["plan"], "write_scope": ["a.py"]}
    nodes = [N("plan", params={"expand": [spec, dict(spec, id="impl.y")]})]
    e = make_engine(tmp_path, nodes)
    e.run()
    assert status(e)["plan"] == "failed"
    assert "impl.x" not in e.state.graph.nodes


def test_stop_file_then_resume(tmp_path):
    e = make_engine(tmp_path, [N("a", params={"sleep": 0.3}), N("b", ["a"])], max_parallel=1)
    threading.Timer(0.1, lambda: (e.state.run_dir / "STOP").write_text("x", encoding="utf-8")).start()
    assert e.run() == "stopped"
    assert status(e) == {"a": "succeeded", "b": "pending"}  # in-flight work finished, nothing new started
    (e.state.run_dir / "STOP").unlink()
    st = RunState.load(e.state.run_dir)
    e2 = Engine(st, e.workspace, AutoApprover(), AuditLog(st.run_dir / "audit.jsonl", "t"), OfflineProvider(),
                PolicyEngine(), Playbook(), config=EngineConfig(verbose=False))
    assert e2.run() == "succeeded"
    assert verify(st.run_dir / "audit.jsonl")[0]


def test_budget_exhaustion_is_a_safe_stop(tmp_path):
    class Broke(Agent):
        name = "broke"

        def run_offline(self, ctx):
            raise BudgetExceeded("LLM token budget exhausted")

    REGISTRY["broke"] = Broke()
    try:
        e = make_engine(tmp_path, [Node("a", "s", "broke"), N("b", ["a"])])
        assert e.run() == "stopped"
        assert "budget" in e.state.stop_reason
    finally:
        REGISTRY.pop("broke")


def test_budget_counts_calls_and_tokens():
    b = Budget(max_calls=2, max_tokens=100)
    b.charge(call=True)
    b.charge(tokens=60)
    b.charge(call=True)
    with pytest.raises(BudgetExceeded):
        b.charge(call=True)
    with pytest.raises(BudgetExceeded):
        b.charge(tokens=50)
