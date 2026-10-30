"""Greenfield: build the URL shortener from an empty repository.

Offline, the implementer replays the reference implementation in service/,
one layer per task, so every task is gated by its own tests and the layers are
assembled in dependency order (parallel where the plan allows).
"""

from __future__ import annotations

from ..workspace import Change
from .base import Playbook, service_file

SRC = "src/UrlShortener"
TESTS = "tests/UrlShortener.Tests"

# task id -> (files the task owns, test files that gate it)
TASK_FILES = {
    "foundation": ([
        "UrlShortener.sln", f"{SRC}/UrlShortener.csproj", f"{TESTS}/UrlShortener.Tests.csproj",
        f"{SRC}/Settings.cs", f"{SRC}/Clock.cs", f"{SRC}/Models.cs", f"{SRC}/Program.cs",
        "openapi.yaml", f"{TESTS}/TestSupport.cs",
    ], [f"{TESTS}/ModelsTests.cs"]),
    "codegen": ([f"{SRC}/CodeGenerator.cs"], [f"{TESTS}/CodeGeneratorTests.cs"]),
    "persistence": ([f"{SRC}/Migrations.cs", f"{SRC}/LinkRepository.cs"], [f"{TESTS}/StorageTests.cs"]),
    "analytics": ([f"{SRC}/Analytics.cs"], [f"{TESTS}/AnalyticsTests.cs"]),
    "domain_service": ([f"{SRC}/LinkService.cs"], [f"{TESTS}/LinkServiceTests.cs"]),
    "http_api": ([f"{SRC}/Endpoints.cs", f"{SRC}/Program.cs", f"{TESTS}/TestApp.cs"], [f"{TESTS}/ApiTests.cs"]),
}

# The foundation task needs an entry point so the web project compiles before the
# HTTP layer exists; http_api later replaces it with the real composition root.
SCAFFOLD_PROGRAM = """// Scaffold entry point: replaced by the http_api task with the full composition root.
var builder = WebApplication.CreateBuilder(args);
var app = builder.Build();
app.MapGet("/healthz", () => Results.Json(new { status = "ok", version = "0.1.0" }));
app.Run();

public partial class Program
{
}
"""


class GreenfieldPlaybook(Playbook):
    name = "greenfield"

    def requirements(self, text: str, feedback: list[str]) -> dict:
        def r(i, t, *acc):
            return {"id": i, "text": t, "acceptance": list(acc)}

        return {
            "problem_statement": "Provide an HTTP service that turns long URLs into short links, redirects "
                                 "visitors, and reports click analytics, safe to expose publicly.",
            "functional": [
                r("FR-1", "Create a short link for an http(s) URL with a generated code",
                  "POST /api/v1/links returns 201 with code and shortUrl",
                  "codes are unpredictable and unique"),
                r("FR-2", "Optionally choose a custom alias; an alias in use is never overwritten",
                  "duplicate alias returns 409 and the original link is unchanged",
                  "reserved words and malformed aliases are rejected with 422"),
                r("FR-3", "Redirect GET /{code} to the destination",
                  "302 with Location header and Cache-Control: no-store", "unknown code returns 404"),
                r("FR-4", "Read and delete link metadata",
                  "GET /api/v1/links/{code} returns metadata", "DELETE returns 204 then 404"),
                r("FR-5", "Click analytics per link",
                  "stats return total clicks, clicks by day and top referrer hosts"),
            ],
            "non_functional": [
                r("NFR-1", "Privacy: never persist raw client IPs or full referrer URLs",
                  "IPs stored as salted hashes; referrers reduced to host"),
                r("NFR-2", "Security: only http/https destinations; bounded request size",
                  "javascript:/data: URLs rejected", "bodies over 16 KiB rejected with 413"),
                r("NFR-3", "Reliability: concurrent creates are safe; data persists across restarts",
                  "100 concurrent creates yield unique codes", "alias race has exactly one winner"),
                r("NFR-4", "Operability: health endpoint and request ids",
                  "GET /healthz returns 200", "x-request-id echoed on every response"),
            ],
            "out_of_scope": ["user accounts and authentication", "custom domains", "link editing"],
            "assumptions": ["single-region deployment; SQLite is sufficient for the prototype",
                            "302 (not 301) so every visit is counted"],
            "ambiguities": [],
            "questions": [],
            "keywords": ["link", "redirect", "stats", "click", "alias"],
        }

    def design(self, requirements: dict, analysis: dict | None, feedback: list[str]) -> dict:
        adrs = [
            {"id": "ADR-1", "title": "ASP.NET Core minimal APIs on .NET 10",
             "decision": "Use ASP.NET Core minimal APIs (net10.0, current LTS) with explicit validation",
             "rationale": "Small surface, no controller ceremony, first-class in-memory testing via "
                          "WebApplicationFactory.",
             "alternatives": ["MVC controllers (more ceremony for 5 endpoints)", "Azure Functions (cold starts)"]},
            {"id": "ADR-2", "title": "SQLite behind a repository interface",
             "decision": "Persist through ILinkRepository with a Microsoft.Data.Sqlite implementation (WAL mode)",
             "rationale": "Zero-ops for the prototype; the interface is the seam for Postgres/SQL Server later.",
             "alternatives": ["Postgres now (ops cost before it is needed)", "In-memory (no durability)"]},
            {"id": "ADR-3", "title": "Random base62 codes with bounded collision retry",
             "decision": "7-char codes from a CSPRNG; retry up to 5 times on collision, then 503",
             "rationale": "3.5e12 keyspace, not enumerable; bounded retries cannot loop forever.",
             "alternatives": ["Sequential ids (enumerable)", "Hash of URL (same URL always same code; leaks)"]},
            {"id": "ADR-4", "title": "302 redirect with Cache-Control: no-store",
             "decision": "Redirect with 302 and no-store",
             "rationale": "301s are cached by browsers, which silently drops analytics and blocks deletion.",
             "alternatives": ["301 (cheaper, but uncounted and uncorrectable)"]},
            {"id": "ADR-5", "title": "Privacy-preserving analytics",
             "decision": "Store salted SHA-256 IP hashes and referrer hosts only",
             "rationale": "Supports unique-visitor estimates without holding personal data.",
             "alternatives": ["Raw IPs (GDPR exposure)", "No per-click data (no analytics)"]},
            {"id": "ADR-6", "title": "Forward-only versioned migrations",
             "decision": "Append-only SchemaMigrations list applied transactionally at startup",
             "rationale": "Reproducible schema; change-control policy forbids editing applied versions.",
             "alternatives": ["EF Core migrations (heavier; generated code is harder to review)"]},
        ]
        summary = ("Layered ASP.NET Core service: Endpoints (minimal APIs) -> LinkService (rules) -> "
                   "ILinkRepository (SQLite). Code generation, analytics and the clock are injected so every "
                   "layer is testable in isolation.")
        if feedback:
            fb = " ".join(feedback)
            if "scal" in fb.lower():
                adrs.append({
                    "id": "ADR-7", "title": "Scaling path beyond a single node",
                    "decision": "Phase 1 SQLite single node; phase 2 Postgres via ILinkRepository with a "
                                "read-through cache on redirects; phase 3 async click ingestion (queue) so "
                                "redirect latency is independent of analytics writes",
                    "rationale": "Redirects dominate traffic and are read-only; the repository seam and "
                                 "click recording isolation make each phase a local change.",
                    "alternatives": ["Start on Postgres + Redis (premature for prototype load)"]})
            summary += f" Revised per review: {fb}"
        comps = [
            {"name": "HTTP API", "module": "Endpoints.cs / Program.cs", "responsibility": "routing, validation, errors"},
            {"name": "LinkService", "module": "LinkService.cs", "responsibility": "business rules"},
            {"name": "Repository", "module": "LinkRepository.cs", "responsibility": "persistence"},
            {"name": "Migrations", "module": "Migrations.cs", "responsibility": "schema versions"},
            {"name": "CodeGen", "module": "CodeGenerator.cs", "responsibility": "short codes"},
            {"name": "Analytics", "module": "Analytics.cs", "responsibility": "click capture"},
            {"name": "Models/Config", "module": "Models.cs / Settings.cs", "responsibility": "schemas, settings"},
        ]
        return {
            "summary": summary, "components": comps, "adrs": adrs,
            "api": [{"method": "POST", "path": "/api/v1/links", "purpose": "create"},
                    {"method": "GET", "path": "/{code}", "purpose": "redirect"},
                    {"method": "GET", "path": "/api/v1/links/{code}", "purpose": "metadata"},
                    {"method": "DELETE", "path": "/api/v1/links/{code}", "purpose": "delete"},
                    {"method": "GET", "path": "/api/v1/links/{code}/stats", "purpose": "analytics"}],
            "data_model": ["links(code PK, url, created_at)",
                           "clicks(id, code FK cascade, clicked_at, referrer_host, ip_hash, user_agent)"],
            "traceability": {"FR-1": ["HTTP API", "LinkService", "CodeGen"], "FR-2": ["LinkService", "Repository"],
                             "FR-3": ["HTTP API"], "FR-4": ["HTTP API", "Repository"],
                             "FR-5": ["Analytics", "Repository"]},
        }

    def threats(self, requirements: dict) -> list[dict]:
        return [
            {"id": "R1", "category": "Tampering", "risk": "Custom alias overwrites another user's link (hijack)",
             "likelihood": "medium", "impact": "high", "mitigation": "INSERT without REPLACE; 409 on conflict",
             "verified_by": ["tests/UrlShortener.Tests/LinkServiceTests.cs::Alias_conflict_does_not_hijack_existing_link",
                             "tests/UrlShortener.Tests/ApiTests.cs::Create_with_alias_and_conflict"]},
            {"id": "R2", "category": "Spoofing", "risk": "javascript:/data: destinations used for XSS via redirect",
             "likelihood": "high", "impact": "high", "mitigation": "scheme allow-list (http, https) + host required",
             "verified_by": ["tests/UrlShortener.Tests/ModelsTests.cs::Rejects_unsafe_or_malformed_urls",
                             "tests/UrlShortener.Tests/ApiTests.cs::Create_validation_errors"]},
            {"id": "R3", "category": "Information disclosure", "risk": "Analytics store personal data",
             "likelihood": "high", "impact": "high", "mitigation": "salted IP hash; referrer host only; UA truncated",
             "verified_by": ["tests/UrlShortener.Tests/AnalyticsTests.cs::Build_click_never_stores_raw_ip_or_full_referrer"]},
            {"id": "R4", "category": "Tampering", "risk": "Concurrent alias claims both succeed",
             "likelihood": "low", "impact": "high", "mitigation": "primary-key constraint is the arbiter",
             "verified_by": ["tests/UrlShortener.Tests/IntegrationTests.cs::Concurrent_alias_race_has_single_winner"]},
            {"id": "R5", "category": "Information disclosure", "risk": "Sequential codes allow link enumeration",
             "likelihood": "medium", "impact": "medium", "mitigation": "CSPRNG base62 codes",
             "verified_by": ["tests/UrlShortener.Tests/CodeGeneratorTests.cs::Codes_are_not_trivially_repeated"]},
            {"id": "R6", "category": "Denial of service", "risk": "Oversized bodies exhaust memory",
             "likelihood": "medium", "impact": "medium", "mitigation": "16 KiB body cap, 2048-char URL cap",
             "verified_by": ["tests/UrlShortener.Tests/ApiTests.cs::Create_rejects_oversized_body"]},
            {"id": "R7", "category": "Tampering", "risk": "SQL injection through codes or URLs",
             "likelihood": "low", "impact": "medium", "mitigation": "parameterized queries; policy blocks "
             "interpolated SQL", "verified_by": []},
        ]

    def plan(self, requirements: dict, design: dict, analysis: dict | None) -> list[dict]:
        def t(i, title, deps, reqs, desc, risk="medium"):
            files, tests = TASK_FILES[i]
            return {"id": i, "title": title, "deps": deps, "files": files, "tests": tests,
                    "requirements": reqs, "description": desc, "risk": risk}

        return [
            t("foundation", "Solution scaffolding, config, request models", [], ["FR-2", "NFR-2"],
              "Solution and projects, settings from env, request validation (scheme allow-list, alias rules), "
              "OpenAPI contract, scaffold entry point."),
            t("codegen", "Short-code generator", ["foundation"], ["FR-1"], "CSPRNG base62 codes, injectable for tests."),
            t("persistence", "Migrations and SQLite repository", ["foundation"], ["FR-2", "FR-4", "NFR-3"],
              "Versioned migrations, repository protocol, alias conflicts as exceptions.", "high"),
            t("analytics", "Privacy-preserving click capture", ["foundation"], ["FR-5", "NFR-1"],
              "Salted IP hash, referrer host extraction, UA truncation."),
            t("domain_service", "LinkService business rules", ["codegen", "persistence", "analytics"],
              ["FR-1", "FR-2", "FR-5"], "Shorten with bounded collision retry, resolve, record clicks, stats."),
            t("http_api", "HTTP API and entrypoint", ["domain_service"], ["FR-1", "FR-3", "FR-4", "FR-5", "NFR-4"],
              "Minimal API endpoints, error envelope, body limits, request ids, DI composition root.", "high"),
        ]

    def implement(self, task: dict, feedback: list[str]) -> list[Change]:
        files, tests = TASK_FILES[task["id"]]
        changes = []
        for path in files + tests:
            if task["id"] == "foundation" and path.endswith("Program.cs"):
                changes.append(Change(path=path, content=SCAFFOLD_PROGRAM))
            else:
                changes.append(Change(path=path, content=service_file(path)))
        return changes

    def integration_tests(self) -> list[Change]:
        path = f"{TESTS}/IntegrationTests.cs"
        return [Change(path=path, content=service_file(path))]

    def changelog(self, requirements: dict) -> str:
        return "- Initial release of the URL shortener service (v1.0.0)."
