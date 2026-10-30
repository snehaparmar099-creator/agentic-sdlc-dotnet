# Scenarios

The orchestrator is Python; the product in every scenario is the **C# / .NET 10** URL shortener,
built and tested with `dotnet build` / `dotnet test` as the quality gates.

All three run with `python -m agentic demo` (scripted reviewer decisions, offline agents), or one
at a time with `python -m agentic run <name>` to make the decisions yourself. The scenario files
in `scenarios/` hold the request, the seed codebase, the scripted reviewer decisions and the
injected faults, so every behaviour described here is reproducible and covered by
`tests/test_llm_and_scenarios.py` (stubbed toolchain everywhere; real `dotnet` when installed).

---

## 1. Greenfield: build the URL shortener

**Request.** Create short links (optional custom alias), redirect, read/delete metadata, click
analytics; publicly exposed, so validate input and keep personal data out of analytics.

### Requirement understanding
9 requirements with acceptance criteria: FR-1..FR-5 (create, alias without hijack, redirect,
metadata/delete, analytics) and NFR-1..NFR-4 (privacy, input security, concurrency/durability,
operability). Out of scope and assumptions are recorded (e.g. 302 rather than 301 so every visit
counts).

### Decomposition (architect's task plan)

| Wave | Tasks (parallel within a wave) |
|---|---|
| 1 | `foundation` (solution + projects, settings, request models, OpenAPI, scaffold entry point) |
| 2 | `codegen` ∥ `persistence` (migrations, SQLite repository) ∥ `analytics` (privacy-preserving click capture) |
| 3 | `domain_service` (business rules) |
| 4 | `http_api` (routes, error envelope, limits, request ids) |

Each task owns disjoint files and ships its own xUnit tests. Its exit gates are `dotnet build`
(the whole solution must compile) and its own test class (`dotnet test --filter`). The
foundation ships a minimal scaffold `Program.cs` so the web project compiles from the first
task; `http_api` later replaces it with the real composition root. Builds are serialized
(parallel `dotnet build`s in one tree would clash on `obj/`), so parallel tasks overlap in
agent work, not in compilation.

### Orchestration highlights
- **Threat model ∥ requirements-driven design.** 7 risks, 4 high impact; each high-impact risk
  names the tests that must prove its mitigation.
- **Design rejected by a reviewer.** The principal engineer asks for the scaling path; the
  architect revises (adds ADR-7: SQLite → Postgres behind `ILinkRepository` → async click
  ingestion) and the second version is approved. Both versions and the human decision are in the
  decision lineage.
- **Injected regression.** On `impl.http_api` attempt 1 the fault injector corrupts the create
  endpoint (201 → 200), simulating a plausible agent mistake. The task's exit gate runs its tests,
  2 fail, the changes are rolled back, attempt 2 passes. Metrics show 1 retry, 1 rollback and the
  MTTR.
- **Join.** `test` waits for all six tasks and runs the full suite (about 60 test cases);
  then the security review; then the release agent writes docs and the go/no-go checkpoint.

### Validation
Per-task gates, full suite, independent policy re-scan of every changed file, risk register
verification (6/7 verified; R7 SQL injection is mitigated by policy-enforced parameterized
queries and recorded as such), docs/OpenAPI drift check, 8-item release checklist → GO.

---

## 2. Brownfield: link expiry + inflated analytics bug

**Seed.** The existing .NET service (`service/`) and its xUnit suite.
**Request.** Expiring links (410 Gone, expiry visible in metadata) and a bug: HEAD requests and
link-preview bots "(Slackbot, Twitterbot, facebookexternalhit, etc.)" are counted as clicks;
existing clients must keep working.

### Requirement understanding
FR-1..FR-4 plus NFR-1 (backward compatible API) and NFR-2 (safe schema change). The vague "etc."
is flagged and resolved by a recorded low-impact assumption (curated UA marker list; reversible
because user agents are stored) rather than a blocking question.

### Codebase reasoning (analyst, deterministic)
Lexical analysis of the C# seed: declared types and methods per file, 6 minimal-API routes,
3 tables, and a dependency graph (which files reference which other files' types). Keyword
matches (`expire`, `click`, `stats`, `redirect`, `analytics`) expanded through the reverse
dependency graph give the impacted files (Models, LinkRepository, Migrations, Analytics,
LinkService, Endpoints) and the test classes that cover them; the baseline suite is run and recorded before any edit. Data flow:
HTTP route → LinkService → LinkRepository → SQLite, with the redirect path writing clicks.

### Decomposition

| Wave | Tasks |
|---|---|
| 1 | `data_layer` (migration 2, record fields, TTL validation, bot-excluding queries) ∥ `bot_detection` (new `BotDetector` class) |
| 2 | `service_rules` (TTL → expires_at, `is_expired`, bot flag on click capture) |
| 3 | `http_layer` (410, HEAD not counted, clock injection, OpenAPI) |

Changes are **targeted edits** with exact-once anchors to existing files (a stale anchor fails the
attempt instead of clobbering code), plus new test classes. The existing tests are not
modified, which is how NFR-1 is verified.

### Orchestration highlights
- **Change control.** `data_layer` adds a schema migration. Policy verifies `SchemaMigrations` in `Migrations.cs` is
  append-only (editing an applied migration would be blocked) and holds the change for approval;
  the DBA approves; the engine then applies and gates it.
- **Retries exhausted → fallback.** Three simulated provider timeouts on `impl.service_rules`
  exhaust its retry budget; the engine switches to the fallback agent (`implementer:offline`) with
  a fresh budget and it succeeds (1 fallback, MTTR recorded).
- **Rollback plan.** The release agent notes the migration is additive, so application rollback
  needs no down-migration.

### Validation
About 90 test cases (the existing suite plus ~30 new), including a v1-database in-place upgrade test, risk register
4/5 verified (R5 "bots spoof browser UAs" explicitly accepted), release GO.

---

## 3. Ambiguous: "make it safer and protect it from abuse"

**Seed.** The existing service. **Request.** "Security flagged that our public URL shortener is
being abused. Make it safer for public use and protect it from abuse."

### Requirement understanding
"safer" and "abuse" are flagged as unmeasurable. Instead of guessing, the requirements agent
asks three multiple-choice questions, each with a recommended option and rationale:

| Question | Options | Scripted answer |
|---|---|---|
| Q1 Which abuse? | bulk creation / internal-network destinations / both / malware feeds | both |
| Q2 How to identify and limit clients? | per-IP in-process / per-IP Redis / per API key | per-IP in-process |
| Q3 Limit? | 20/min burst 10, 60/min burst 20, 5/min burst 5 | 20/min burst 10 |

Answers become part of the requirements output, so they flow into the threat model, design and task plan
and are part of the decision lineage. Choosing a narrower Q1 answer produces a different plan
(tested: bulk-only yields `rate_limiter → api_integration` and FR-2 is explicitly descoped, not
silently dropped). Options the offline playbook cannot implement are recorded as explicit
fallbacks in the design summary.

### Orchestration highlights
- **Amendment → re-plan.** At design review the security lead rejects with an amendment to the
  *requirements*: also block destinations that are short links on our own domain (redirect
  chains). The engine re-runs requirements (reusing the earlier clarification answers), marks
  every descendant stale, and on completion invalidates the threat model, analysis and design
  because their inputs changed (2 invalidations; the design itself is re-run). The re-derived design adds ADR-15, the threat
  model adds R5, and the new plan's `url_safety` task carries the extra requirement.
- **Change control on a dependency.** The first `rate_limiter` proposal over-reaches: a Redis
  limiter "for later" plus the `StackExchange.Redis` NuGet package in the `.csproj`. Policy holds the new dependency; the
  platform owner rejects ("no new infrastructure"); the agent revises with that feedback and the
  in-process limiter ships without the dependency.
- **Brownfield regression caught by design.** Rate limiting would break the existing concurrent
  integration test (100 creates from one client). The `api_integration` task explicitly updates
  that load-style test to opt out of limits, which the full-suite gate then validates.

### Validation
About 95 test cases: token bucket (burst, refill, isolation, bounded key table, thread safety), destination
policy (private/reserved ranges, obfuscated IP literals, internal hostnames, own short domain,
public destinations allowed), API behaviour (429 + Retry-After, redirects never limited, 422
`unsafe_destination`). Risk register 6/7 verified; R7 "public hostname resolving to a private IP"
is an accepted, documented trade-off (no DNS at create time). Release GO.

---

## Running a scenario interactively

```bash
python -m agentic run ambiguous
```

You will be asked the clarification questions, then to review the design and task plan, any
change-control holds and the release. At each checkpoint: `a` approve, `r` reject with feedback
(the agent revises with your feedback), `s` stop safely. Use `--no-faults` to disable the
scenario's injected faults, `--auto-approve` for an unattended run.

To write a new scenario, copy one of the JSON files: `requirement`, `type`
(`greenfield|brownfield|ambiguous`), `seed` (directory to start from, or null), `playbook`,
optional `approval_script`, `faults`, `policy_overrides`, `llm_budget`. With an API key set, the
LLM-capable agents work from the requirement text; the playbook is the fallback.
