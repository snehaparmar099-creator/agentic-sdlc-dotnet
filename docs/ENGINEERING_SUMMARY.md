# Engineering Summary

## 1. Plan and rationale

**Problem as I framed it.** The interesting part of "agentic SDLC" is not getting a model to emit
code; it is making multi-step agent work *governable*: knowing what will run before it runs,
bounding what an agent may change, verifying outputs independently, letting humans intervene at
the right points, recovering from failure without corrupting state, and leaving an audit trail
that explains every outcome. The URL shortener is the vehicle; the orchestrator is the product.

**Approach.**
1. Build the target service first, properly (layered ASP.NET Core / SQLite service in C#, with an
   xUnit suite), so the
   agents' output has a real quality bar and the scenarios have a real codebase to change.
2. Build the orchestration core around one rule: *agents propose, the engine disposes*. All
   writes, policy, gates, commits, approvals and state transitions happen in the engine.
3. Make the workflow an explicit dependency graph with entry/exit gates and checkpoints, extended
   at runtime by the architect's task plan under validation, with stateful re-planning when upstream outputs
   change.
4. Make every failure path real and observable: retries with backoff, per-attempt rollback,
   fallback agents, human escalation, safe stop, resume, run-level rollback, and metrics.
5. Prove it with three scenarios that each force a different governance path, deterministic
   enough to be tested end to end.

**Build order.** service → graph/state/audit/metrics → workspace/policy/approvals/LLM → agents →
engine → scenarios → reports → tests → docs.

## 2. Artifacts

| Artifact | Where |
|---|---|
| Orchestrator (runnable) | `agentic/` (`python -m agentic ...`) |
| SDLC workflow template | `agentic/pipeline.py` |
| Guardrail configuration | `agentic/policies.json` |
| Scenarios | `scenarios/*.json` |
| Reference .NET service + API schema | `service/UrlShortener.sln`, `service/src/UrlShortener/`, `service/openapi.yaml` |
| Tests | `tests/` (orchestrator, incl. scenario flows; real-`dotnet` runs when the SDK is present), `service/tests/UrlShortener.Tests/` (xUnit) |
| Architecture | `docs/ARCHITECTURE.md` |
| Scenario walkthroughs | `docs/SCENARIOS.md` |
| Per-run outputs | `runs/<id>/`: workspace (git), `report.md`, `ENGINEERING_SUMMARY.md`, `artifacts/RELEASE_NOTES.md`, `audit.jsonl`, `metrics.json`, `state.json` |

Generated per run by agents: requirements spec with acceptance criteria, ADRs (`docs/adr/` in the
workspace), threat model, task plan, code and tests, README, CHANGELOG, release notes with
requirement → task → test traceability and a rollback plan.

## 3. Risks, trade-offs and validation

### Risks in the system itself, and controls

| Risk | Control | Verified by |
|---|---|---|
| Agent writes outside its task or touches secrets/CI | Write-scope + protected-path policy, path confinement in the workspace | `test_scope_rules` |
| Agent introduces insecure code or credentials | Content rules on introduced lines; independent re-scan of the whole diff at review | `test_blocking_rules`, scenario reviews |
| Bad output lands in the workspace | Snapshot before apply, exit gates, restore on failure | `test_failing_exit_gate_restores_workspace`, greenfield fault |
| Parallel tasks clobber each other | Task plan validated: concurrent nodes must have disjoint write scopes | `test_invalid_expansion_with_overlapping_scopes_fails_planner` |
| Unreviewed schema or dependency change | Change-control holds; applied migrations immutable | `test_migrations_are_append_only`, brownfield/ambiguous holds |
| Infinite loops (retries, revisions) | Bounded attempts, bounded revisions, bounded LLM calls/tokens | `test_revision_limit_is_enforced`, `test_budget_*` |
| Stale work after requirements change | Fingerprint-based invalidation with rollback; unchanged nodes revalidated | `test_amendment_invalidates_changed_downstream_and_rolls_back`, ambiguous scenario |
| Unattended run pushes past a failure or self-certifies | Auto approver always stops on failure escalation; unattended approvals do not satisfy the release checklist | `test_unrecovered_failure_safe_stops_unattended_and_skips_nothing_else`, `test_unattended_run_cannot_certify_a_release` |
| Tampered audit trail | Hash chain + verifier | `test_audit_chain_detects_tampering` |
| Prompt injection via repo content | Untrusted content quoted as data; outputs schema-validated and policy-checked, never executed | `test_llm_output_is_validated_and_used` |
| Model returns garbage | Validation → agent-level fallback to playbook; node-level fallback after retries | `test_invalid_llm_output_falls_back_to_playbook`, brownfield fallback |

### Trade-offs
See `docs/ARCHITECTURE.md` §8. The main ones:
- **Explicit DAG over free-form agent loops**: predictable and governable; less exploratory.
- **Deterministic reviewer/release agents**: the generator does not grade itself; reviews are
  less nuanced than an LLM reviewer would be.
- **Threads and file-based state**: zero infrastructure and easy resume; one orchestrator
  process per run.
- **Service**: SQLite and in-process rate limiting are right for one instance (and documented as
  the scaling path in ADR-7); 302 over 301 trades redirect caching for accurate analytics and
  deletability.

### Testing approach
- **Unit**: graph algorithms, every policy rule (positive and negative), workspace edit
  composition and rollback, audit tamper detection, budget accounting.
- **Engine behaviour** with scripted fake agents: parallelism and joins (timing-asserted),
  retry/MTTR, fallback, unattended safe stop, abandon-and-skip, exit-gate rollback, reviewer
  revision and revision limit, policy hold approve/reject, policy block never applied, amendment
  invalidation with file rollback, dynamic expansion and join wiring, invalid expansion, STOP file
  + resume with intact audit chain, budget exhaustion.
- **LLM path** with a stub provider: valid output used and quoted inputs; invalid output falls
  back.
- **End to end**: each scenario runs to GO in a temp directory; metrics show the expected
  governance events; audit chain verifies; run-level rollback returns the workspace to baseline;
  a different clarification answer changes the plan.
- **Generated code**: every run's own exit gates build the solution and execute the produced xUnit
  tests (about 60 / 90 / 95 test cases for greenfield / brownfield / ambiguous).
- **Two-mode scenario tests**: the orchestrator's scenario tests run against a stubbed toolchain on
  any machine (orchestration logic) and against the real .NET SDK when installed (compilation and
  behaviour).

## 4. Assumptions
- Python 3.11+, git and the .NET 10 SDK available; the orchestrator runs on a developer machine or CI runner.
- The orchestrator is Python (agent tooling is most mature there); the product is .NET. The
  engine is language-agnostic; only the toolchain, policy rules and analyst indexer are per-language.
- The service runs as a single instance (drives SQLite and the in-process limiter).
- Client identity for rate limiting is the connection IP; production deployments run behind a
  trusted proxy with `uvicorn --proxy-headers`.
- Scripted approvals stand in for people in the demo; interactive mode is the intended use.
- Human reviewers are authorized for the checkpoints they answer (no RBAC in the prototype).

## 5. Limitations and what I would do next

| Limitation | Next step |
|---|---|
| Offline implementation replays curated playbook code; only the orchestration, validation and governance are "live" without an API key | Run with an API key; extend LLM-generated plans with an implementer that does not depend on playbook task ids for its fallback |
| If the LLM architect invents task ids, the offline fallback for those tasks cannot help; the failure escalates to a human | Make the fallback re-plan with the deterministic planner instead of failing the task |
| Node timeouts are enforced for test runs and HTTP calls, not as a hard kill of a running agent thread | Run agents in subprocesses with hard deadlines |
| Metrics are per process: a resumed run starts a new metrics window | Persist counters in state and merge on resume |
| Approvals have identity (`cli:<user>`, `script:<role>`) but no authentication/RBAC or approval quorum for high-impact changes | Integrate with SSO and require two approvers for `impact=high` holds |
| Bot detection is UA-based; no DNS resolution for destination checks | Documented accepted risks R5 (brownfield) and R7 (ambiguous) |
| Single-process orchestration | Move state to Postgres and the scheduler to a durable queue (Temporal-style) for multi-run, multi-host operation |
| Security review is rule-based | Add Roslyn analyzers / SAST (semgrep) and `dotnet list package --vulnerable` as additional gates |
| C# analysis is lexical, not semantic | Use Roslyn for a precise symbol graph |
| Builds are serialized across parallel tasks | Per-task worktrees so tasks can compile concurrently |
