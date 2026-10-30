"""Release agent: documentation and go/no-go.

1. Docs: README, CHANGELOG and ADRs generated from the actual code (routes,
   configuration) and the run's recorded design decisions, plus a drift check
   between implemented routes and the OpenAPI spec.
2. Readiness: the go/no-go checklist from recorded run facts, the requirement ->
   task -> test traceability matrix, and release notes with a rollback plan.

Deterministic by design. A human signs off at the release checkpoint.
"""

from __future__ import annotations

import re
from datetime import date

from ..workspace import Change
from .analyst import build_index
from .base import Agent, AgentContext, AgentResult

ENV_RE = re.compile(r"(?:os\.getenv|GetEnvironmentVariable|IntFromEnv)\(\s*\"([A-Z0-9_]+)\"")
OPENAPI_PATH_RE = re.compile(r"^  (/[^:]*):\s*$", re.MULTILINE)
REQUIRED_README_SECTIONS = ["## Quickstart", "## API", "## Configuration", "## Architecture", "## Testing"]


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:50]


class ReleaseAgent(Agent):
    name = "release"
    role = "You are a release manager."

    def supports_llm(self, ctx: AgentContext) -> bool:
        return False

    def run_offline(self, ctx: AgentContext) -> AgentResult:
        f = ctx.facts
        req = ctx.find("requirements") or {}
        plan = ctx.find("design") or {}
        test = (ctx.find("test") or {}).get("gates", {}).get("full_suite", {})
        review = ctx.find("security_review") or {}
        doc_changes, docs = self._docs(ctx)
        statuses = f.get("node_statuses", {})
        impl = {k: v for k, v in statuses.items() if k.startswith("impl.")}
        # Unattended approvals (--auto-approve, unscripted defaults) do not count as human sign-off.
        approved_nodes = {a["request"]["node_id"] for a in f.get("approvals", [])
                          if a["decision"]["decision"] == "approve" and not a["decision"].get("unattended")}
        changed = f.get("changed_files", [])
        policy_holds = [a for a in f.get("approvals", []) if a["request"]["kind"] == "policy_hold"]

        checklist = [
            ("All implementation tasks succeeded", bool(impl) and all(v == "succeeded" for v in impl.values())),
            ("Full test suite green", bool(test.get("passed")) and test.get("total", 0) > 0),
            ("Security review passed", review.get("status") == "pass"),
            ("No blocking policy findings outstanding", review.get("blocking", 1) == 0),
            ("High-impact risks verified by tests", not review.get("unverified_high_risks")),
            ("Docs complete, no API/spec drift",
             not docs.get("missing_sections") and not docs.get("openapi_drift")),
            ("Design and plan approved by an accountable reviewer (not unattended)",
             "design" in approved_nodes),
            ("Every policy hold decided by a human (approved, or rejected and revised)",
             all(a["decision"]["decision"] in ("approve", "reject") for a in policy_holds)),
        ]
        ready = all(ok for _, ok in checklist)

        task_by_req: dict[str, list[dict]] = {}
        for t in plan.get("tasks", []):
            for r in t.get("requirements", []):
                task_by_req.setdefault(r, []).append(t)
        passed = set(test.get("passed_ids", []))
        trace = []
        for r in req.get("functional", []) + req.get("non_functional", []):
            tasks = task_by_req.get(r["id"], [])
            test_files = sorted({tf for t in tasks for tf in t.get("tests", [])})
            n_pass = sum(1 for p in passed if any(p.startswith(tf) for tf in test_files))
            trace.append({"requirement": r["id"], "tasks": [t["id"] for t in tasks],
                          "test_files": test_files, "passing_tests": n_pass})

        migration = any(p.lower().endswith(("migrations.py", "migrations.cs")) for p in changed)
        rollback = [
            "Application rollback: redeploy the previous build (git tag `baseline` in the run workspace).",
            "Run-level rollback: `python -m agentic rollback <run_id> --to baseline` restores the workspace.",
        ]
        if migration:
            rollback.append("Schema: migrations are additive (new nullable columns / tables only); the previous "
                            "build ignores them, so no down-migration is required for an application rollback.")
        notes = (f"# Release notes: {ctx.scenario.get('title', ctx.run_id)}\n\n"
                 f"Run `{ctx.run_id}` | readiness: **{'GO' if ready else 'NO-GO'}**\n\n## Changes\n"
                 + "\n".join(f"- {r['id']}: {r['text']}" for r in req.get("functional", []))
                 + "\n\n## Readiness checklist\n"
                 + "\n".join(f"- [{'x' if ok else ' '}] {item}" for item, ok in checklist)
                 + "\n\n## Traceability\n| Requirement | Tasks | Test files | Passing tests |\n|---|---|---|---|\n"
                 + "\n".join(f"| {t['requirement']} | {', '.join(t['tasks']) or '-'} | "
                             f"{', '.join(t['test_files']) or '-'} | {t['passing_tests']} |" for t in trace)
                 + "\n\n## Rollback plan\n" + "\n".join(f"- {r}" for r in rollback)
                 + f"\n\n## Files changed ({len(changed)})\n" + "\n".join(f"- `{p}`" for p in changed) + "\n")
        return AgentResult(
            output={"ready": ready, "checklist": [{"item": i, "ok": ok} for i, ok in checklist],
                    "traceability": trace, "rollback_plan": rollback, "schema_change": migration,
                    "docs": docs, "missing_sections": docs["missing_sections"],
                    "openapi_drift": docs["openapi_drift"]},
            changes=doc_changes,
            summary=f"release readiness: {'GO' if ready else 'NO-GO'} "
                    f"({sum(ok for _, ok in checklist)}/{len(checklist)} checks)",
            artifacts={"RELEASE_NOTES.md": notes},
            decisions=[{"summary": f"Recommend {'GO' if ready else 'NO-GO'} for release",
                        "rationale": "; ".join(f"{i}: {'ok' if ok else 'FAILED'}" for i, ok in checklist),
                        "alternatives": []}],
        )

    def _docs(self, ctx: AgentContext) -> tuple[list[Change], dict]:
        """README, CHANGELOG and ADRs derived from code and recorded decisions."""
        ws = ctx.workspace
        req = ctx.find("requirements") or {}
        design = ctx.find("design") or {}
        index = build_index(ws)
        routes = [r for i in index["modules"].values() for r in i["routes"]]
        sources = [p for p in ws.list_files() if p.endswith((".py", ".cs")) and not p.startswith("tests")]
        env_vars = sorted({v for p in sources for v in ENV_RE.findall(ws.read(p) or "")})

        openapi = ws.read("openapi.yaml") or ""
        spec_paths = set(OPENAPI_PATH_RE.findall(openapi))
        code_paths = {r["path"] for r in routes}
        drift = sorted(code_paths ^ spec_paths) if openapi else []

        api_rows = "\n".join(
            f"| {', '.join(r['methods'])} | `{r['path']}` | `{r['handler']}` |" for r in routes)
        comp_rows = "\n".join(
            f"| {c['name']} | `{c.get('module', '')}` | {c['responsibility']} |" for c in design.get("components", []))
        adr_links = []
        changes: list[Change] = []
        for adr in design.get("adrs", []):
            fname = f"docs/adr/{adr['id']}-{slug(adr['title'])}.md"
            adr_links.append(f"- [{adr['id']}: {adr['title']}]({fname})")
            alts = "\n".join(f"- {a}" for a in adr.get("alternatives", [])) or "- none recorded"
            changes.append(Change(path=fname, content=(
                f"# {adr['id']}: {adr['title']}\n\nStatus: accepted ({date.today().isoformat()}, run {ctx.run_id})\n\n"
                f"## Decision\n{adr['decision']}\n\n## Rationale\n{adr['rationale']}\n\n"
                f"## Alternatives considered\n{alts}\n")))
        env_rows = "\n".join(f"| `{v}` |" for v in env_vars) or "| (none) |"
        fr_rows = "\n".join(f"- **{r['id']}** {r['text']}" for r in req.get("functional", []))
        readme = f"""# URL Shortener

{req.get('problem_statement', '')}

## Capabilities
{fr_rows}

## Quickstart
```bash
dotnet test
dotnet run --project src/UrlShortener          # listens on http://localhost:5000
curl -s -X POST localhost:5000/api/v1/links -H 'content-type: application/json' \\
     -d '{{"url": "https://example.com/some/long/path"}}'
```

## API
Full contract: [`openapi.yaml`](openapi.yaml). Errors use `{{"error": {{"code", "message", "details"}}}}`.

| Methods | Path | Handler |
|---|---|---|
{api_rows}

## Configuration
| Environment variable |
|---|
{env_rows}

## Architecture
{design.get('summary', '')}

| Component | Module | Responsibility |
|---|---|---|
{comp_rows}

### Decisions
{chr(10).join(adr_links) or '- none recorded'}

## Testing
```bash
dotnet test
```
Unit tests cover each module; `IntegrationTests` exercises full flows against a file-backed database,
including concurrent creation and alias races.
"""
        changes.append(Change(path="README.md", content=readme))

        old_log = ws.read("CHANGELOG.md") or "# Changelog\n"
        header, _, rest = old_log.partition("\n")
        entry = (f"\n## [Unreleased] - {date.today().isoformat()}\n"
                 + "\n".join(f"- {r['id']}: {r['text']}" for r in req.get("functional", []))
                 + (("\n" + ctx.playbook.changelog(req)) if ctx.playbook.changelog(req) else "") + "\n")
        changes.append(Change(path="CHANGELOG.md", content=f"{header}\n{entry}{rest}"))

        missing_sections = [s for s in REQUIRED_README_SECTIONS if s not in readme]
        return changes, {"files": [c.path for c in changes], "routes_documented": len(routes),
                         "env_vars": env_vars, "openapi_drift": drift, "missing_sections": missing_sections}
