"""Entry and exit gates.

Entry gates run before a node is scheduled (preconditions on upstream state).
Exit gates run after the node's changes are applied; a failing exit gate rolls
the node's changes back and counts as a failed attempt. Gates are independent
of the agent: the agent's own claims about its output are never trusted.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    from .agents.base import AgentResult
    from .engine import Engine
    from .graph import Node


def _ok(message: str = "ok", **extra) -> dict:
    return {"passed": True, "message": message, **extra}


def _fail(message: str, **extra) -> dict:
    return {"passed": False, "message": message, **extra}


def _approved(engine: "Engine", node_id: str) -> bool:
    decisions = [a for a in engine.state.approvals
                 if a["request"]["node_id"] == node_id and a["request"]["kind"] == "stage_output"]
    return (node_id in engine.state.nodes
            and engine.state.nodes[node_id].status == "succeeded"
            and bool(decisions) and decisions[-1]["decision"]["decision"] == "approve")


# -- entry gates -------------------------------------------------------------
def design_approved(engine, node, result=None) -> dict:
    return _ok() if _approved(engine, "design") else _fail("design and plan have not been approved by a human")


def tests_green(engine, node, result=None) -> dict:
    out = engine.state.nodes.get("test")
    gate = ((out.output or {}).get("gates", {}) if out else {}).get("full_suite", {})
    return _ok() if gate.get("passed") else _fail("full test suite is not green")


def review_passed(engine, node, result=None) -> dict:
    out = engine.state.nodes.get("security_review")
    return _ok() if out and (out.output or {}).get("status") == "pass" else _fail("security review not passed")


# -- exit gates --------------------------------------------------------------
def compiles(engine, node, result: "AgentResult") -> dict:
    if engine.workspace.toolchain == "dotnet":
        rep = engine.workspace.build()
        if rep.passed:
            return _ok(f"dotnet build succeeded in {rep.duration_s}s")
        detail = "\n".join(rep.errors) or rep.output_tail[-1500:]
        return _fail(f"dotnet build failed:\n{detail}")
    errors = []
    for ch in result.changes:
        if ch.path.endswith(".py") and not ch.delete:
            src = engine.workspace.read(ch.path) or ""
            try:
                compile(src, ch.path, "exec")
            except SyntaxError as exc:
                errors.append(f"{ch.path}:{exc.lineno}: {exc.msg}")
    return _fail("; ".join(errors)) if errors else _ok()


def task_tests(engine, node, result: "AgentResult") -> dict:
    tests = [t for t in node.params.get("task", {}).get("tests", []) if engine.workspace.exists(t)]
    if not tests:
        return _fail("task declares no runnable tests")
    rep = engine.workspace.run_tests(tests)
    msg = f"{len(rep.passed_ids)} passed, {len(rep.failed_ids)} failed"
    if not rep.passed:
        return _fail(f"{msg}: {rep.failed_ids[:5]}\n{rep.output_tail[-1500:]}", **rep.to_dict())
    return _ok(msg, **{k: v for k, v in rep.to_dict().items() if k != "output_tail"})


def full_suite(engine, node, result: "AgentResult") -> dict:
    rep = engine.workspace.run_tests()
    minimum = engine.policy.cfg.get("min_tests", 1)
    msg = f"{len(rep.passed_ids)}/{rep.total} passed in {rep.duration_s}s"
    if not rep.passed:
        return _fail(f"{msg}: {rep.failed_ids[:5]}\n{rep.output_tail[-1500:]}", **rep.to_dict())
    if rep.total < minimum:
        return _fail(f"only {rep.total} tests; policy requires {minimum}", **rep.to_dict())
    return _ok(msg, **{k: v for k, v in rep.to_dict().items() if k != "output_tail"})


def review_clean(engine, node, result: "AgentResult") -> dict:
    out = result.output
    if out.get("status") == "pass":
        return _ok()
    return _fail(f"blocking findings={out.get('blocking')}, unverified high risks={out.get('unverified_high_risks')}")


def docs_complete(engine, node, result: "AgentResult") -> dict:
    out = result.output
    problems = []
    if out.get("missing_sections"):
        problems.append(f"README missing {out['missing_sections']}")
    if out.get("openapi_drift"):
        problems.append(f"routes and openapi.yaml disagree on {out['openapi_drift']}")
    return _fail("; ".join(problems)) if problems else _ok()


def release_ready(engine, node, result: "AgentResult") -> dict:
    failed = [c["item"] for c in result.output.get("checklist", []) if not c["ok"]]
    return _fail(f"readiness checks failed: {failed}") if failed else _ok()


ENTRY_GATES: dict[str, Callable] = {
    "design_approved": design_approved,
    "tests_green": tests_green,
    "review_passed": review_passed,
}

EXIT_GATES: dict[str, Callable] = {
    "compiles": compiles,
    "task_tests": task_tests,
    "full_suite": full_suite,
    "review_clean": review_clean,
    "docs_complete": docs_complete,
    "release_ready": release_ready,
}
