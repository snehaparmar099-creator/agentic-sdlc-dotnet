"""Implementer and test agents."""

from __future__ import annotations

from ..llm import as_data
from ..workspace import Change
from .base import Agent, AgentContext, AgentError, AgentResult

IMPL_SCHEMA = """{"files": [{"path": str, "content": "full file content"}], "notes": [str]}"""


class ImplementerAgent(Agent):
    name = "implementer"
    role = ("You are a senior C#/.NET engineer. Write production-quality, nullable-aware code with xUnit "
            "tests. Only touch the files the task owns. Never use Process.Start, BinaryFormatter, disabled "
            "TLS validation, or SQL built with string interpolation; always use parameterized queries.")

    def _finish(self, task: dict, changes: list[Change], tokens=(0, 0), notes=None) -> AgentResult:
        if not changes:
            raise AgentError(f"task {task['id']} produced no changes")
        paths = sorted({c.path for c in changes})
        return AgentResult(
            output={"task": task["id"], "files": paths},
            summary=f"{task['id']}: {len(paths)} file(s)",
            changes=changes,
            decisions=[{"summary": f"Implemented {task['id']}: {task['title']}",
                        "rationale": task.get("description", ""), "alternatives": []}],
            notes=notes or [],
            tokens=tokens,
        )

    def run_offline(self, ctx: AgentContext) -> AgentResult:
        task = ctx.node.params["task"]
        try:
            changes = ctx.playbook.implement(task, ctx.feedback)
        except KeyError as exc:
            raise AgentError(f"no playbook implementation for task {task['id']}") from exc
        return self._finish(task, changes)

    def run_llm(self, ctx: AgentContext) -> AgentResult:
        task = ctx.node.params["task"]
        blocks = [as_data("task", str(task))]
        blocks += self.upstream_block(ctx, "design")
        for path in sorted(set(task["files"]) | set(task["tests"]) | set(task.get("context_files", []))):
            content = ctx.workspace.read(path)
            if content is not None:
                blocks.append(as_data(f"current:{path}", content))
        data, tokens = self.ask_json(
            ctx, f"Implement task '{task['id']}'. Return the complete content of every file you change "
                 f"(you may only write: {sorted(set(task['files']) | set(task['tests']))}). Include tests.",
            blocks, IMPL_SCHEMA, max_tokens=8000)
        changes = [Change(path=f["path"], content=f["content"]) for f in data.get("files", [])]
        return self._finish(task, changes, tokens, data.get("notes"))


class TestAgent(Agent):
    name = "tester"
    role = "You are an SDET writing end-to-end and regression tests."

    def supports_llm(self, ctx: AgentContext) -> bool:
        return False  # integration tests come from the playbook; per-task tests come from implementers

    def run_offline(self, ctx: AgentContext) -> AgentResult:
        changes = ctx.playbook.integration_tests()
        plan = ctx.find("design") or {}
        return AgentResult(
            output={"integration_test_files": [c.path for c in changes],
                    "tasks_under_test": [t["id"] for t in plan.get("tasks", [])]},
            summary=f"{len(changes)} integration test file(s); full suite runs as exit gate",
            changes=changes,
            decisions=[{"summary": "Run the entire suite (unit + integration) as the stage exit gate",
                        "rationale": "Per-task gates prove each unit in isolation; only the full suite "
                                     "proves the units compose.", "alternatives": []}],
        )
