"""SDLC workflow template and run setup.

    requirements                      (clarification checkpoint if the request is ambiguous)
        ├── threat_model              security agent, mode=threat_model
        └── analysis                  codebase analyst (brownfield / ambiguous only)
    design 🔒                          architect: design + task plan, one human checkpoint;
        │                             expands into impl.* nodes at runtime
    impl.a ∥ impl.b → impl.c ...      implementer per task, own tests as exit gate
    test                              join: waits for every impl.* node; full suite gate
    security_review                   security agent, mode=review (deterministic)
    release 🔒                         docs + readiness checklist, human go/no-go
"""

from __future__ import annotations

import json
import secrets
import shutil
import time
from pathlib import Path

from .approvals import AutoApprover, CliApprover, ScriptedApprover
from .audit import AuditLog
from .engine import Engine, EngineConfig, FaultInjector
from .graph import Node, RetryPolicy
from .llm import Budget, make_provider
from .playbooks.base import REPO_ROOT, get_playbook
from .policy import PolicyEngine
from .state import RunState
from .workspace import Workspace

RUNS_DIR = REPO_ROOT / "runs"


def build_graph(scenario: dict) -> list[Node]:
    brownfield = scenario.get("type") in ("brownfield", "ambiguous") or scenario.get("seed")
    design_deps = ["requirements", "threat_model"] + (["analysis"] if brownfield else [])
    nodes = [
        Node("requirements", "requirements", "requirements", impact="high",
             fallback_agent="requirements:offline"),
        Node("threat_model", "design", "security", deps=["requirements"], params={"mode": "threat_model"},
             fallback_agent="security:offline"),
    ]
    if brownfield:
        nodes.append(Node("analysis", "analysis", "analyst", deps=["requirements"],
                          fallback_agent="analyst:offline"))
    nodes += [
        Node("design", "design", "architect", deps=design_deps, approval=True, impact="high",
             fallback_agent="architect:offline"),
        Node("test", "testing", "tester", deps=["design"], params={"join_dynamic_from": "design"},
             write_scope=["tests/test_integration*.py", "tests/*/IntegrationTests.cs"], exit_gates=["full_suite"],
             impact="medium", retry=RetryPolicy(max_attempts=2)),
        Node("security_review", "review", "security", deps=["test"], params={"mode": "review"},
             exit_gates=["review_clean"], entry_gates=["tests_green"], impact="high",
             retry=RetryPolicy(max_attempts=1)),
        Node("release", "release", "release", deps=["security_review"], approval=True, impact="high",
             write_scope=["README.md", "CHANGELOG.md", "docs/*"], entry_gates=["tests_green", "review_passed"],
             exit_gates=["docs_complete", "release_ready"], retry=RetryPolicy(max_attempts=1)),
    ]
    return nodes


def load_scenario(path: str | Path) -> dict:
    path = Path(path)
    if not path.exists():
        candidate = REPO_ROOT / "scenarios" / f"{path.stem if path.suffix else path.name}.json"
        path = candidate
    return json.loads(path.read_text(encoding="utf-8"))


def resolve_seed(scenario: dict, from_run: str | None) -> Path | None:
    if from_run:
        ws = RUNS_DIR / from_run / "workspace"
        if not ws.exists():
            raise FileNotFoundError(f"run {from_run} has no workspace")
        return ws
    seed = scenario.get("seed")
    return (REPO_ROOT / seed) if seed else None


def make_approver(mode: str, scenario: dict):
    if mode == "auto":
        return AutoApprover()
    if mode == "scripted":
        return ScriptedApprover(scenario.get("approval_script", []), fallback=AutoApprover("script:default"))
    return CliApprover()


def create_run(scenario: dict, *, approvals: str = "cli", llm_mode: str = "auto", max_parallel: int = 4,
               from_run: str | None = None, rollback_on_stop: bool = False, faults: bool = True,
               run_id: str | None = None, verbose: bool = True, runs_dir: Path | None = None) -> Engine:
    runs_dir = runs_dir or RUNS_DIR
    run_id = run_id or f"{time.strftime('%Y%m%d-%H%M%S')}-{scenario['id']}-{secrets.token_hex(2)}"
    toolchain = scenario.get("toolchain", "pytest")
    if toolchain == "dotnet" and shutil.which("dotnet") is None:
        raise SystemExit(
            "This scenario builds a .NET service, but the 'dotnet' command was not found.\n"
            "Install the .NET 10 SDK (Windows: winget install Microsoft.DotNet.SDK.10), "
            "restart your terminal, and check with: dotnet --version")
    run_dir = runs_dir / run_id
    run_dir.mkdir(parents=True, exist_ok=False)
    config = {"approvals": approvals, "llm": llm_mode, "max_parallel": max_parallel,
              "from_run": from_run, "rollback_on_stop": rollback_on_stop, "faults": faults}
    state = RunState(run_dir, run_id, scenario, config)
    for node in build_graph(scenario):
        state.add_node(node)
    state.graph.validate()

    audit = AuditLog(run_dir / "audit.jsonl", run_id)
    ws = Workspace(run_dir / "workspace", toolchain=toolchain)
    baseline = ws.init(resolve_seed(scenario, from_run))
    audit.emit("workspace_initialized", seed=str(resolve_seed(scenario, from_run)), baseline=baseline)
    state.save()
    return _engine(state, ws, audit, scenario, config, verbose)


def _engine(state, ws, audit, scenario, config, verbose) -> Engine:
    budget = Budget(**scenario.get("llm_budget", {}))
    return Engine(
        state=state, workspace=ws, audit=audit,
        approver=make_approver(config["approvals"], scenario),
        llm=make_provider(config["llm"], budget),
        policy=PolicyEngine(scenario.get("policy_overrides")),
        playbook=get_playbook(scenario["playbook"]),
        faults=FaultInjector(scenario.get("faults") if config.get("faults", True) else []),
        config=EngineConfig(max_parallel=config["max_parallel"],
                            rollback_on_stop=config.get("rollback_on_stop", False), verbose=verbose),
    )


def load_run(run_id: str, approvals: str | None = None, verbose: bool = True,
             runs_dir: Path | None = None) -> Engine:
    run_dir = (runs_dir or RUNS_DIR) / run_id
    state = RunState.load(run_dir)
    if approvals:
        state.config["approvals"] = approvals
    for nid, ns in state.nodes.items():
        if ns.status in ("running", "awaiting_approval"):
            ns.status = "pending"
    stop = run_dir / "STOP"
    if stop.exists():
        stop.unlink()
    state.stop_reason = None
    audit = AuditLog(run_dir / "audit.jsonl", run_id)
    audit.emit("run_resumed")
    ws = Workspace(run_dir / "workspace", toolchain=state.scenario.get("toolchain", "pytest"))
    return _engine(state, ws, audit, state.scenario, state.config, verbose)
