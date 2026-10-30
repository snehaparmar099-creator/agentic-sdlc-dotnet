"""Architect: design the solution and decompose it into implementation tasks.

One agent, one human checkpoint: the reviewer approves the design and the task
plan together. The plan becomes part of the workflow graph at runtime: each
task is a node `impl.<task>` depending on the design node and on its
prerequisite tasks. Tasks without a dependency path between them run in
parallel; the test node is the synchronization point that waits for all of them.
"""

from __future__ import annotations

import json

from ..llm import as_data
from .base import Agent, AgentContext, AgentError, AgentResult, NodeSpec

DESIGN_SCHEMA = """{
  "summary": str,
  "components": [{"name": str, "responsibility": str, "module": str}],
  "api": [{"method": str, "path": str, "purpose": str}],
  "data_model": [str],
  "adrs": [{"id": "ADR-1", "title": str, "decision": str, "rationale": str, "alternatives": [str]}],
  "traceability": {"FR-1": ["component name"]},
  "descoped": {"FR-x": "reason (only if the reviewer's answers exclude it)"}
}"""

PLAN_SCHEMA = """{"tasks": [{"id": "snake_case", "title": str, "description": str,
  "deps": ["other_task_id"], "files": ["src/UrlShortener/File.cs"], "tests": ["tests/UrlShortener.Tests/FileTests.cs"],
  "requirements": ["FR-1"], "risk": "low|medium|high"}]}"""


def validate_design(design: dict, requirements: dict) -> dict:
    for key in ("summary", "components", "adrs"):
        if not design.get(key):
            raise AgentError(f"design missing {key}")
    for adr in design["adrs"]:
        if not all(adr.get(k) for k in ("id", "title", "decision", "rationale")):
            raise AgentError(f"incomplete ADR: {adr}")
    trace = design.setdefault("traceability", {})
    descoped = design.setdefault("descoped", {})
    missing = [r["id"] for r in requirements.get("functional", [])
               if r["id"] not in trace and r["id"] not in descoped]
    if missing:
        raise AgentError(f"design neither traces nor explicitly descopes requirements: {missing}")
    design.setdefault("api", [])
    design.setdefault("data_model", [])
    return design


def validate_tasks(tasks: list[dict], requirements: dict, descoped: dict) -> list[dict]:
    if not tasks:
        raise AgentError("plan has no tasks")
    ids = [t.get("id") for t in tasks]
    if len(set(ids)) != len(ids) or not all(ids):
        raise AgentError(f"task ids must be unique and non-empty: {ids}")
    for t in tasks:
        for d in t.get("deps", []):
            if d not in ids:
                raise AgentError(f"task {t['id']} depends on unknown task {d}")
        if not t.get("files"):
            raise AgentError(f"task {t['id']} declares no files")
        t.setdefault("tests", [])
        t.setdefault("requirements", [])
        t.setdefault("deps", [])
    covered = {r for t in tasks for r in t["requirements"]}
    missing = [r["id"] for r in requirements.get("functional", [])
               if r["id"] not in covered and r["id"] not in descoped]
    if missing:
        raise AgentError(f"plan leaves requirements unimplemented: {missing}")
    return tasks


def waves(tasks: list[dict]) -> list[list[str]]:
    """Group tasks into parallel waves (for the plan summary)."""
    remaining = {t["id"]: set(t["deps"]) for t in tasks}
    done: set[str] = set()
    out = []
    while remaining:
        wave = sorted(t for t, deps in remaining.items() if deps <= done)
        if not wave:
            raise AgentError(f"task dependency cycle among {sorted(remaining)}")
        out.append(wave)
        done.update(wave)
        for t in wave:
            remaining.pop(t)
    return out


class ArchitectAgent(Agent):
    name = "architect"
    role = ("You are a principal engineer. Produce a minimal, defensible design (record each significant "
            "choice as an ADR), then decompose it into small, independently testable tasks with explicit "
            "dependencies and non-overlapping file ownership so independent tasks can run in parallel.")

    def _finish(self, ctx: AgentContext, design: dict, tasks: list[dict], tokens=(0, 0)) -> AgentResult:
        req = ctx.find("requirements") or {}
        design = validate_design(design, req)
        tasks = validate_tasks(tasks, req, design["descoped"])
        plan_waves = waves(tasks)
        if ctx.feedback:
            design["review_feedback_addressed"] = list(ctx.feedback)
        brownfield = ctx.scenario.get("type") != "greenfield"
        specs = [
            NodeSpec(
                id=f"impl.{t['id']}",
                stage="implementation",
                agent="implementer",
                deps=[ctx.node.id] + [f"impl.{d}" for d in t["deps"]],
                params={"task": t, "brownfield": brownfield},
                write_scope=sorted(set(t["files"]) | set(t["tests"])),
                exit_gates=["compiles", "task_tests"],
                entry_gates=["design_approved"],
                impact="high" if t.get("risk") == "high" else "medium",
            )
            for t in tasks
        ]
        decisions = [{"summary": f"{a['id']}: {a['title']} -> {a['decision']}", "rationale": a["rationale"],
                      "alternatives": a.get("alternatives", [])} for a in design["adrs"]]
        decisions.append({
            "summary": f"{len(tasks)} tasks in {len(plan_waves)} waves: "
                       + " | ".join(", ".join(w) for w in plan_waves),
            "rationale": "Tasks own disjoint files so independent ones execute concurrently; "
                         "each carries its own tests as an exit gate.",
            "alternatives": ["Single monolithic implementation step (rejected: no isolation for retry/rollback)"],
        })
        output = dict(design, tasks=tasks, waves=plan_waves)
        return AgentResult(
            output=output, new_nodes=specs, decisions=decisions, tokens=tokens,
            summary=f"{len(design['components'])} components, {len(design['adrs'])} ADRs, "
                    f"{len(tasks)} tasks in {len(plan_waves)} parallel waves",
        )

    def run_offline(self, ctx: AgentContext) -> AgentResult:
        req, analysis = ctx.find("requirements") or {}, ctx.find("analysis")
        design = ctx.playbook.design(req, analysis, ctx.feedback)
        tasks = ctx.playbook.plan(req, design, analysis)
        return self._finish(ctx, design, tasks)

    def run_llm(self, ctx: AgentContext) -> AgentResult:
        context = self.upstream_block(ctx, "requirements", "analysis", "threat_model")
        design, t1 = self.ask_json(
            ctx, "Design the solution for these requirements. Trace every functional requirement to at "
                 "least one component, or descope it only if the reviewer's clarification answers exclude it.",
            context, DESIGN_SCHEMA)
        plan, t2 = self.ask_json(
            ctx, "Decompose this design into implementation tasks. Every in-scope functional requirement "
                 "must be covered. Tasks that can run in parallel must not share files.",
            context + [as_data("design", json.dumps(design))], PLAN_SCHEMA)
        return self._finish(ctx, design, plan.get("tasks", []), (t1[0] + t2[0], t1[1] + t2[1]))
