# Agentic SDLC Orchestrator: URL Shortener

A working prototype of an agentic software-delivery system. A requirement goes in; a reviewed,
tested, documented, release-ready change comes out, produced by agents that work inside explicit
autonomy boundaries while humans own approvals and final quality.

**Two parts, two languages:**
- **The orchestrator** (`agentic/`) is Python, where the agent tooling is most mature.
- **The product it builds** is a **C# / .NET 10** URL shortener (`service/`): ASP.NET Core minimal
  APIs, SQLite via Microsoft.Data.Sqlite, xUnit tests. The orchestrator's quality gates run
  `dotnet build` and `dotnet test` on every change.

The orchestrator is language-agnostic about what it builds: pointing it at another stack means
swapping the build/test gate and the language-specific policy rules, not redesigning it.

The system is demonstrated on the URL shortener built three ways:

| Scenario | Request | What it demonstrates |
|---|---|---|
| **Greenfield** | Build the shortener from an empty repo | Decomposition into 6 tasks across 4 parallel waves, a design rejected and revised by a reviewer, an injected agent regression caught by an exit gate and rolled back |
| **Brownfield** | Add link expiry and fix inflated click analytics in the existing service | Static codebase analysis and impact set, targeted patch edits, a schema migration held by change control until a human approves, retries exhausted then fallback agent |
| **Ambiguous** | "Make it safer and protect it from abuse" | Clarifying questions, a reviewer amendment that re-plans the run (upstream stages invalidated and re-derived), a new dependency held and rejected by change control |

```
requirement ─► requirements ─► {threat model ∥ codebase analysis} ─► design + task plan 🔒
             ─► impl.* (parallel, per-task test gates) ─► test (join, full suite)
             ─► security review ─► release: docs + go/no-go 🔒
```
🔒 = human checkpoint. Additional checkpoints are raised dynamically for clarifications, policy holds and unrecovered failures.

## Quickstart

Requires **Python 3.11+**, **git**, and the **.NET 10 SDK** (`dotnet --version` should print 10.x).

```bash
python -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt

# 1. Check the .NET service builds and its tests pass on your machine (first run restores NuGet packages)
cd service && dotnet test && cd ..

# 2. Run all three scenarios unattended (scripted reviewer decisions, offline agents)  a few minutes
python -m agentic demo

# Run one scenario and make the decisions yourself at each checkpoint
python -m agentic run greenfield            # brownfield | ambiguous | path/to/scenario.json
```

**Windows / PyCharm.** Install [Git for Windows](https://git-scm.com/download/win), Python 3.11+, and
the .NET 10 SDK (`winget install Microsoft.DotNet.SDK.10`); restart PyCharm afterwards so it sees
`git` and `dotnet` on PATH. Open the folder in PyCharm, create a virtualenv interpreter, run
`pip install -r requirements.txt` in the PyCharm terminal, then either use the terminal commands
above (with `.venv\Scripts\activate`) or create a Run Configuration: *Module name* `agentic`,
*Parameters* `demo` (or `run ambiguous`), *Working directory* the project root. For interactive
approvals, run it in the terminal or tick *Emulate terminal in output console*.

Each run writes to `runs/<run_id>/`:

| Path | Contents |
|---|---|
| `workspace/` | the produced code and tests: a git repo with one commit per successful node, `baseline` tag = start |
| `report.md` | run record: final graph, node table, human checkpoints, decision lineage, policy findings, timeline, metrics |
| `ENGINEERING_SUMMARY.md` | plan and rationale, ADRs, risks and how each was verified, assumptions, limitations |
| `artifacts/RELEASE_NOTES.md` | readiness checklist, requirement-to-test traceability, rollback plan |
| `audit.jsonl` | hash-chained audit log of every event |
| `state.json`, `metrics.json` | resumable run state; reliability metrics |

Try the produced service:

```bash
cd runs/<greenfield_run_id>/workspace
dotnet test
dotnet run --project src/UrlShortener            # http://localhost:5000
curl -s -X POST localhost:5000/api/v1/links -H 'content-type: application/json' -d '{"url":"https://example.com/x","alias":"demo"}'
curl -si localhost:5000/demo | head -3
curl -s localhost:5000/api/v1/links/demo/stats
```

## Command reference

```bash
python -m agentic run <scenario> [--approvals cli|auto|scripted] [--auto-approve]
                                 [--llm auto|offline|anthropic] [--max-parallel N]
                                 [--from-run <run_id>] [--rollback-on-stop] [--no-faults]
python -m agentic status <run_id>
python -m agentic stop <run_id>              # safe stop: in-flight nodes finish, nothing new starts
python -m agentic resume <run_id>            # continue a stopped run from saved state
python -m agentic rollback <run_id> --to baseline|<node_id>
python -m agentic verify-audit <run_id>      # detects any edit, deletion or reordering of audit events
python -m agentic metrics [run_id ...]       # aggregate reliability metrics across runs
```

**Approval modes.** `cli` (default) prompts you at every checkpoint: approve, reject with feedback
(the agent revises), or stop. `scripted` replays the reviewer decisions recorded in the scenario
file, so demos and CI are deterministic. `auto` approves stage outputs unattended so the agents
can produce and validate everything, but it never pushes past an unrecovered failure, and
unattended approvals do not count as sign-off: the release checklist requires design
approval by an accountable reviewer, so an `--auto-approve` run stops at release with NO-GO.
Every decision records who made it and whether it was unattended.

**LLM modes.** With `ANTHROPIC_API_KEY` set, `--llm auto` makes the requirements, analyst
(risk notes), threat-model, architect and implementer agents call the model; output is schema-validated and falls
back to the deterministic playbook when invalid. Model: `AGENTIC_MODEL` (default
`claude-sonnet-5-5`). Calls and tokens are capped per run (`llm_budget` in the scenario file);
exhausting the budget triggers a safe stop. Without a key, agents run offline from playbooks.
The security review, test and release steps are deterministic by design. Other providers plug in
by subclassing `LLMProvider`.

## Tests

The orchestrator's scenario tests run in two modes: against a stubbed toolchain (always, so the
orchestration logic is testable anywhere) and against the real .NET SDK when `dotnet` is installed.

```bash
python -m pytest           # orchestrator: graph, policy, audit, workspace, engine, LLM path, scenario flows
(cd service && dotnet test)   # the .NET URL shortener (xUnit: unit + in-memory HTTP + integration)
```

## Repository layout

```
agentic/            orchestrator
  engine.py         scheduler, outcome handling, retries/fallback/escalation, invalidation, safe stop
  graph.py          DAG, topo order, parallel write-scope conflict detection, mermaid export
  state.py          persistent run state, cross-stage context, decision lineage
  workspace.py      git-backed workspace: apply/snapshot/restore, commits, dotnet build/test (TRX) runner
  policy.py         guardrails (policies.json): scope, secrets, dangerous C# APIs, interpolated SQL,
                    PII logging, NuGet packages, append-only migrations
  gates.py          entry/exit gates
  approvals.py      CLI / auto / scripted human checkpoints
  audit.py          hash-chained audit log + verifier
  metrics.py        success rate, retries, rollbacks, MTTR, latency
  llm.py            Anthropic provider (stdlib HTTP), budget, JSON extraction
  agents/           7 agents: requirements, analyst, security (threat model + review),
                    architect (design + plan), implementer, tester, release (docs + go/no-go)
  playbooks/        offline domain knowledge per scenario
  pipeline.py       SDLC workflow template, run creation/resume
  report.py         report.md and ENGINEERING_SUMMARY.md
scenarios/          greenfield.json, brownfield.json, ambiguous.json
service/            the .NET URL shortener (greenfield target; brownfield/ambiguous seed)
  UrlShortener.sln
  src/UrlShortener/          ASP.NET Core minimal API, SQLite repository, migrations, analytics
  tests/UrlShortener.Tests/  xUnit tests, incl. in-memory HTTP tests via WebApplicationFactory
tests/              orchestrator tests
docs/               ARCHITECTURE.md, SCENARIOS.md, ENGINEERING_SUMMARY.md
```

Start with [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md), then [docs/SCENARIOS.md](docs/SCENARIOS.md);
[docs/ENGINEERING_SUMMARY.md](docs/ENGINEERING_SUMMARY.md) covers risks, trade-offs, testing approach,
assumptions and limitations.
