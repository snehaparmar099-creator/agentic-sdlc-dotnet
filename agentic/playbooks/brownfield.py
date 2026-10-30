"""Brownfield: enhance and fix the existing .NET service (seeded from service/).

Change request: link expiry (feature) + inflated click analytics (bug: HEAD
requests and link-preview bots are counted as human clicks).

Implementation is expressed as targeted edits (find/replace anchors) to the
existing C# files plus new test classes, the way a reviewer would see a PR: an
anchor that no longer matches exactly once makes the change inapplicable and
fails the attempt instead of silently clobbering code.
"""

from __future__ import annotations

from ..workspace import Change
from .base import Playbook

SRC = "src/UrlShortener"
TESTS = "tests/UrlShortener.Tests"


def edit(path: str, find: str, replace: str) -> Change:
    return Change(path=path, find=find, replace=replace)


TASKS = {
    "data_layer": ([f"{SRC}/Migrations.cs", f"{SRC}/Models.cs", f"{SRC}/LinkRepository.cs"],
                   [f"{TESTS}/StorageV2Tests.cs"]),
    "bot_detection": ([f"{SRC}/BotDetector.cs"], [f"{TESTS}/BotDetectorTests.cs"]),
    "service_rules": ([f"{SRC}/LinkService.cs", f"{SRC}/Analytics.cs"], [f"{TESTS}/LinkServiceExpiryTests.cs"]),
    "http_layer": ([f"{SRC}/Endpoints.cs", "openapi.yaml"], [f"{TESTS}/ApiV2Tests.cs"]),
}

STORAGE_V2_TESTS = '''using Microsoft.Data.Sqlite;

namespace UrlShortener.Tests;

public class StorageV2Tests
{
    private static readonly DateTimeOffset T0 = new(2026, 1, 1, 12, 0, 0, TimeSpan.Zero);

    private static void Exec(SqliteConnection connection, string sql)
    {
        using var command = connection.CreateCommand();
        command.CommandText = sql;
        command.ExecuteNonQuery();
    }

    [Fact]
    public void V1_database_upgrades_in_place()
    {
        var path = Path.Combine(Path.GetTempPath(), $"v1-{Guid.NewGuid():N}.db");
        using (var connection = new SqliteConnection($"Data Source={path}"))
        {
            connection.Open();
            Exec(connection, "CREATE TABLE schema_version (version INTEGER PRIMARY KEY)");
            Exec(connection, SchemaMigrations.All[0].Sql);
            Exec(connection, "INSERT INTO schema_version VALUES (1)");
            Exec(connection, "INSERT INTO links VALUES ('legacy', 'https://old.example', '2026-01-01T12:00:00.0000000+00:00')");
            Exec(connection, "INSERT INTO clicks (code, clicked_at) VALUES ('legacy', '2026-01-01T12:00:00.0000000+00:00')");
        }

        using var repo = new SqliteLinkRepository(path);
        var link = repo.Get("legacy")!;
        Assert.Equal("https://old.example", link.Url);
        Assert.Null(link.ExpiresAt);
        Assert.Equal(1, repo.Stats("legacy")!.TotalClicks);  // pre-existing clicks count as human
    }

    [Fact]
    public void Schema_is_at_latest_version()
    {
        using var connection = new SqliteConnection("Data Source=:memory:");
        connection.Open();
        Assert.Equal(2, SchemaMigrations.Apply(connection));
    }

    [Fact]
    public void Expires_at_roundtrip()
    {
        using var repo = new SqliteLinkRepository(":memory:");
        var expires = T0.AddHours(1);
        repo.Create(new Link("ttl", "https://example.com", T0, expires));
        Assert.Equal(expires, repo.Get("ttl")!.ExpiresAt);
    }

    [Fact]
    public void Bot_clicks_excluded_from_human_analytics()
    {
        using var repo = new SqliteLinkRepository(":memory:");
        repo.Create(new Link("b", "https://example.com", T0));
        repo.RecordClick(new Click("b", T0, "t.co", "h", "Mozilla/5.0"));
        repo.RecordClick(new Click("b", T0, "slack.com", "h", "Slackbot-LinkExpanding", IsBot: true));
        repo.RecordClick(new Click("b", T0, "slack.com", "h", "Slackbot-LinkExpanding", IsBot: true));

        var stats = repo.Stats("b")!;
        Assert.Equal(1, stats.TotalClicks);
        Assert.Equal(2, stats.BotClicks);
        Assert.Equal(1, stats.ClicksByDay["2026-01-01"]);
        Assert.Single(stats.TopReferrers);
        Assert.Equal(new ReferrerCount("t.co", 1), stats.TopReferrers[0]);
    }

    [Fact]
    public void Link_expiry_check()
    {
        var link = new Link("x", "https://e.example", T0, T0.AddMinutes(5));
        Assert.False(link.IsExpired(T0));
        Assert.True(link.IsExpired(T0.AddMinutes(5)));
        Assert.False(new Link("y", "https://e.example", T0).IsExpired(T0.AddDays(9999)));
    }
}
'''

BOT_DETECTOR_TESTS = '''namespace UrlShortener.Tests;

public class BotDetectorTests
{
    [Theory]
    [InlineData("Slackbot-LinkExpanding 1.0 (+https://api.slack.com/robots)")]
    [InlineData("Twitterbot/1.0")]
    [InlineData("facebookexternalhit/1.1")]
    [InlineData("LinkedInBot/1.0")]
    [InlineData("Mozilla/5.0 (compatible; Googlebot/2.1)")]
    [InlineData("WhatsApp/2.23")]
    [InlineData("Discordbot/2.0")]
    [InlineData("TelegramBot (like TwitterBot)")]
    [InlineData("curl/8.4.0")]
    public void Known_preview_bots_and_tools(string userAgent)
    {
        Assert.True(BotDetector.IsBot(userAgent));
    }

    [Theory]
    [InlineData("Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/605.1.15 Safari/605.1.15")]
    [InlineData("Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/126.0 Safari/537.36")]
    [InlineData("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) Mobile/15E148")]
    public void Browsers_are_human(string userAgent)
    {
        Assert.False(BotDetector.IsBot(userAgent));
    }

    [Fact]
    public void Missing_user_agent_is_not_classified_as_bot()
    {
        Assert.False(BotDetector.IsBot(null));
        Assert.False(BotDetector.IsBot(""));
    }
}
'''

LINK_SERVICE_EXPIRY_TESTS = '''namespace UrlShortener.Tests;

public class LinkServiceExpiryTests
{
    [Fact]
    public void Ttl_sets_expiry_and_link_expires()
    {
        using var repo = new SqliteLinkRepository(":memory:");
        var clock = new FakeClock();
        var service = new LinkService(repo, TestSettings.InMemory(), clock);

        var link = service.Shorten("https://example.com", null, 3600);
        Assert.Equal(clock.Now.AddSeconds(3600), link.ExpiresAt);
        Assert.False(service.IsExpired(service.Resolve(link.Code)!));

        clock.Advance(TimeSpan.FromSeconds(3600));
        Assert.True(service.IsExpired(service.Resolve(link.Code)!));
    }

    [Fact]
    public void Links_without_ttl_never_expire()
    {
        using var repo = new SqliteLinkRepository(":memory:");
        var clock = new FakeClock();
        var service = new LinkService(repo, TestSettings.InMemory(), clock);

        var link = service.Shorten("https://example.com", "forever");
        clock.Advance(TimeSpan.FromDays(3650));
        Assert.Null(link.ExpiresAt);
        Assert.False(service.IsExpired(service.Resolve("forever")!));
    }

    [Theory]
    [InlineData(0)]
    [InlineData(59)]
    [InlineData(31622401)]
    [InlineData(-5)]
    public void Ttl_bounds_validated(int ttl)
    {
        var errors = LinkValidation.Validate(new CreateLinkRequest("https://example.com", null, ttl), out _);
        Assert.Contains(errors, e => e.Field == "expiresInSeconds");
    }

    [Fact]
    public void Build_click_flags_bots()
    {
        var at = new DateTimeOffset(2026, 1, 1, 0, 0, 0, TimeSpan.Zero);
        var bot = ClickAnalytics.BuildClick("c", at, "1.1.1.1", null, "Twitterbot/1.0", "s");
        var human = ClickAnalytics.BuildClick("c", at, "1.1.1.1", null, "Mozilla/5.0", "s");
        Assert.True(bot.IsBot);
        Assert.False(human.IsBot);
    }
}
'''

API_V2_TESTS = '''using System.Net;
using System.Net.Http.Json;
using System.Text.Json;
using Microsoft.AspNetCore.Mvc.Testing;

namespace UrlShortener.Tests;

public class ApiV2Tests : IDisposable
{
    private readonly FakeClock _clock = new();
    private readonly WebApplicationFactory<Program> _factory;
    private readonly HttpClient _client;

    public ApiV2Tests()
    {
        _factory = TestApp.Create(TestSettings.InMemory(), _clock);
        _client = TestApp.Client(_factory);
    }

    public void Dispose()
    {
        _client.Dispose();
        _factory.Dispose();
    }

    private static async Task<JsonElement> Json(HttpResponseMessage response) =>
        await response.Content.ReadFromJsonAsync<JsonElement>();

    private async Task<string> CreateCode(object body) =>
        (await Json(await _client.PostAsJsonAsync("/api/v1/links", body))).GetProperty("code").GetString()!;

    private Task<HttpResponseMessage> Visit(string code, string? userAgent = null, HttpMethod? method = null)
    {
        var request = new HttpRequestMessage(method ?? HttpMethod.Get, $"/{code}");
        if (userAgent is not null)
        {
            request.Headers.TryAddWithoutValidation("User-Agent", userAgent);
        }

        return _client.SendAsync(request);
    }

    private async Task<JsonElement> Stats(string code) => await Json(await _client.GetAsync($"/api/v1/links/{code}/stats"));

    [Fact]
    public async Task Create_with_expiry_exposes_expires_at()
    {
        var response = await _client.PostAsJsonAsync("/api/v1/links", new { url = "https://example.com", expiresInSeconds = 600 });
        Assert.Equal(HttpStatusCode.Created, response.StatusCode);
        var body = await Json(response);
        var expected = _clock.Now.AddSeconds(600).ToString("o");
        Assert.Equal(expected, body.GetProperty("expiresAt").GetString());

        var meta = await Json(await _client.GetAsync($"/api/v1/links/{body.GetProperty("code").GetString()}"));
        Assert.Equal(expected, meta.GetProperty("expiresAt").GetString());
    }

    [Fact]
    public async Task Expired_link_returns_410()
    {
        var code = await CreateCode(new { url = "https://example.com", expiresInSeconds = 60 });
        Assert.Equal(HttpStatusCode.Redirect, (await Visit(code)).StatusCode);

        _clock.Advance(TimeSpan.FromSeconds(61));
        var expired = await Visit(code);
        Assert.Equal(HttpStatusCode.Gone, expired.StatusCode);
        Assert.Equal("link_expired", (await Json(expired)).GetProperty("error").GetProperty("code").GetString());
        Assert.Equal(HttpStatusCode.OK, (await _client.GetAsync($"/api/v1/links/{code}")).StatusCode);
    }

    [Fact]
    public async Task Invalid_ttl_rejected()
    {
        var response = await _client.PostAsJsonAsync("/api/v1/links", new { url = "https://example.com", expiresInSeconds = 5 });
        Assert.Equal(HttpStatusCode.UnprocessableEntity, response.StatusCode);
    }

    [Fact]
    public async Task Head_requests_redirect_but_are_not_counted()
    {
        var code = await CreateCode(new { url = "https://example.com" });
        Assert.Equal(HttpStatusCode.Redirect, (await Visit(code, method: HttpMethod.Head)).StatusCode);
        var stats = await Stats(code);
        Assert.Equal(0, stats.GetProperty("totalClicks").GetInt32());
        Assert.Equal(0, stats.GetProperty("botClicks").GetInt32());
    }

    [Fact]
    public async Task Bot_clicks_reported_separately()
    {
        var code = await CreateCode(new { url = "https://example.com" });
        await Visit(code, "Slackbot-LinkExpanding 1.0");
        await Visit(code, "facebookexternalhit/1.1");
        await Visit(code, "Mozilla/5.0 (Macintosh) Safari/605.1.15");
        var stats = await Stats(code);
        Assert.Equal(1, stats.GetProperty("totalClicks").GetInt32());
        Assert.Equal(2, stats.GetProperty("botClicks").GetInt32());
    }

    [Fact]
    public async Task Human_clicks_still_counted()
    {
        var code = await CreateCode(new { url = "https://example.com" });
        for (var i = 0; i < 3; i++)
        {
            await Visit(code, "Mozilla/5.0 (iPhone) Mobile/15E148");
        }

        Assert.Equal(3, (await Stats(code)).GetProperty("totalClicks").GetInt32());
    }
}
'''

BOT_DETECTOR = '''namespace UrlShortener;

/// <summary>
/// User-agent based bot classification for click analytics. Link-preview crawlers (chat apps,
/// social networks), search engines and HTTP tools fetch short links without a human visit.
/// Classification is a substring match on the lower-cased user agent; it is intentionally
/// conservative (missing user agents count as human) because misclassifying people as bots
/// hides real traffic, while a missed bot only slightly inflates counts.
/// </summary>
public static class BotDetector
{
    public static readonly string[] Markers =
    {
        "bot", "crawler", "spider", "slurp", "facebookexternalhit", "embedly", "whatsapp",
        "skypeuripreview", "vkshare", "preview", "curl/", "wget/", "python-requests", "httpclient",
        "headlesschrome",
    };

    public static bool IsBot(string? userAgent)
    {
        if (string.IsNullOrEmpty(userAgent))
        {
            return false;
        }

        var ua = userAgent.ToLowerInvariant();
        return Markers.Any(marker => ua.Contains(marker, StringComparison.Ordinal));
    }
}
'''


class BrownfieldPlaybook(Playbook):
    name = "brownfield"

    def requirements(self, text: str, feedback: list[str]) -> dict:
        def r(i, t, *acc):
            return {"id": i, "text": t, "acceptance": list(acc)}

        return {
            "problem_statement": "Extend the existing URL shortener with expiring links and correct click "
                                 "analytics that are inflated by HEAD requests and link-preview bots.",
            "functional": [
                r("FR-1", "Optional expiry when creating a link (expiresInSeconds, 60s..366d)",
                  "201 response and metadata include expiresAt", "out-of-range TTL returns 422"),
                r("FR-2", "Expired links stop redirecting",
                  "GET /{code} after expiry returns 410 link_expired", "metadata remains readable"),
                r("FR-3", "HEAD requests are not counted as clicks", "HEAD /{code} redirects; totalClicks unchanged"),
                r("FR-4", "Bot traffic is visible but excluded from human analytics",
                  "bot user agents increment botClicks, not totalClicks",
                  "clicksByDay and topReferrers count humans only"),
            ],
            "non_functional": [
                r("NFR-1", "Backward compatible API", "existing fields and status codes unchanged; existing "
                  "test suite passes unmodified"),
                r("NFR-2", "Safe schema change", "additive migration only; v1 databases upgrade in place"),
            ],
            "out_of_scope": ["editing expiry after creation", "IP-reputation based bot detection",
                             "re-classifying historical clicks"],
            "assumptions": ["'etc.' bot list resolved as a curated user-agent marker list (low impact, "
                            "reversible without migration since UAs are stored)",
                            "clicks recorded before this change are treated as human"],
            "ambiguities": [],
            "questions": [],
            "keywords": ["expire", "click", "stats", "redirect", "analytics"],
        }

    def design(self, requirements: dict, analysis: dict | None, feedback: list[str]) -> dict:
        impacted = (analysis or {}).get("impact", {}).get("impacted_modules", [])
        adrs = [
            {"id": "ADR-8", "title": "Relative TTL input, absolute expiry storage",
             "decision": "Clients send expiresInSeconds; the service stores absolute UTC expires_at",
             "rationale": "Avoids client clock skew; absolute timestamps are cheap to compare and index.",
             "alternatives": ["Client-supplied expiresAt (skew, past dates)", "Background deletion job"]},
            {"id": "ADR-9", "title": "410 Gone for expired links",
             "decision": "Expired links return 410 with error code link_expired; metadata stays readable",
             "rationale": "Distinguishes 'existed but ended' from 'never existed' for support and clients.",
             "alternatives": ["404 (ambiguous)", "Delete on expiry (loses analytics)"]},
            {"id": "ADR-10", "title": "Additive, backward-compatible migration",
             "decision": "Migration 2 adds nullable links.expires_at and clicks.is_bot DEFAULT 0",
             "rationale": "No backfill or locking rewrite; the previous build ignores the new columns, so "
                          "application rollback needs no down-migration.",
             "alternatives": ["Separate expiry table (extra join on the hot redirect path)"]},
            {"id": "ADR-11", "title": "Classify bots at write time, filter at read time",
             "decision": "Store is_bot per click from user-agent markers; stats exclude bots and report "
                         "botClicks separately",
             "rationale": "Bot traffic stays auditable; classification can evolve since user agents are kept.",
             "alternatives": ["Drop bot clicks (irreversible)", "Classify at read time (slow stats queries)"]},
            {"id": "ADR-12", "title": "HEAD is redirected but not recorded",
             "decision": "HEAD /{code} returns the same 302 without writing a click",
             "rationale": "HEAD is used by preview/uptime tools and never represents a visit.",
             "alternatives": ["405 for HEAD (breaks link checkers)"]},
        ]
        return {
            "summary": "Extend existing layers in place: Models/repository carry ExpiresAt and IsBot, a new "
                       "BotDetector classifies user agents, LinkService owns expiry rules, the endpoints map "
                       "expiry to 410 and skip recording HEAD. Impacted modules from analysis: "
                       + (", ".join(m.split(".")[-1] for m in impacted) or "n/a"),
            "components": [
                {"name": "Migrations", "module": "Migrations.cs", "responsibility": "add migration 2"},
                {"name": "Models", "module": "Models.cs", "responsibility": "ExpiresAt, IsBot, BotClicks"},
                {"name": "Repository", "module": "LinkRepository.cs", "responsibility": "persist + filter bots"},
                {"name": "BotDetector", "module": "BotDetector.cs", "responsibility": "UA classification"},
                {"name": "LinkService", "module": "LinkService.cs", "responsibility": "TTL and expiry rules"},
                {"name": "HTTP API", "module": "Endpoints.cs", "responsibility": "410, HEAD handling"},
            ],
            "adrs": adrs,
            "api": [{"method": "POST", "path": "/api/v1/links", "purpose": "accepts expiresInSeconds"},
                    {"method": "GET", "path": "/{code}", "purpose": "410 when expired"}],
            "data_model": ["links.expires_at TEXT NULL", "clicks.is_bot INTEGER NOT NULL DEFAULT 0"],
            "traceability": {"FR-1": ["Models", "LinkService", "HTTP API"], "FR-2": ["LinkService", "HTTP API"],
                             "FR-3": ["HTTP API"], "FR-4": ["BotDetector", "Repository"]},
        }

    def threats(self, requirements: dict) -> list[dict]:
        return [
            {"id": "R1", "category": "Business logic", "risk": "Expired campaign links keep redirecting",
             "likelihood": "medium", "impact": "high", "mitigation": "expiry enforced in the redirect path",
             "verified_by": [f"{TESTS}/ApiV2Tests.cs::Expired_link_returns_410"]},
            {"id": "R2", "category": "Data integrity", "risk": "Migration fails or corrupts existing data",
             "likelihood": "low", "impact": "high", "mitigation": "additive columns, transactional migration",
             "verified_by": [f"{TESTS}/StorageV2Tests.cs::V1_database_upgrades_in_place"]},
            {"id": "R3", "category": "Business logic", "risk": "Real users misclassified as bots",
             "likelihood": "medium", "impact": "high", "mitigation": "conservative markers; missing UA = human",
             "verified_by": [f"{TESTS}/BotDetectorTests.cs::Browsers_are_human",
                             f"{TESTS}/ApiV2Tests.cs::Human_clicks_still_counted"]},
            {"id": "R4", "category": "Compatibility", "risk": "Existing clients break on response changes",
             "likelihood": "low", "impact": "medium", "mitigation": "additive fields only; v1 suite unmodified",
             "verified_by": [f"{TESTS}/ApiTests.cs::Create_returns_201_with_short_url"]},
            {"id": "R5", "category": "Repudiation", "risk": "Bots spoof browser user agents",
             "likelihood": "medium", "impact": "low", "mitigation": "accepted; documented limitation",
             "verified_by": []},
        ]

    def plan(self, requirements: dict, design: dict, analysis: dict | None) -> list[dict]:
        def t(i, title, deps, reqs, desc, risk="medium"):
            files, tests = TASKS[i]
            return {"id": i, "title": title, "deps": deps, "files": files, "tests": tests,
                    "requirements": reqs, "description": desc, "risk": risk}

        return [
            t("data_layer", "Schema migration 2 and model/repository support", [], ["FR-1", "FR-4", "NFR-2"],
              "Add expires_at/is_bot columns, record fields, TTL validation, bot-excluding stats queries.", "high"),
            t("bot_detection", "User-agent bot classifier", [], ["FR-4"], "New pure class, no dependencies."),
            t("service_rules", "Expiry rules and bot flag on click capture", ["data_layer", "bot_detection"],
              ["FR-1", "FR-2", "FR-4"], "TTL -> ExpiresAt, IsExpired, BuildClick sets IsBot."),
            t("http_layer", "410 for expired links, HEAD not counted, OpenAPI update", ["service_rules"],
              ["FR-2", "FR-3", "NFR-1"], "Redirect endpoint changes; response fields; contract update.", "high"),
        ]

    def implement(self, task: dict, feedback: list[str]) -> list[Change]:
        tid = task["id"]
        if tid == "data_layer":
            return self._data_layer()
        if tid == "bot_detection":
            return [Change(f"{SRC}/BotDetector.cs", BOT_DETECTOR), Change(f"{TESTS}/BotDetectorTests.cs", BOT_DETECTOR_TESTS)]
        if tid == "service_rules":
            return self._service_rules()
        if tid == "http_layer":
            return self._http_layer()
        raise KeyError(tid)

    def _data_layer(self) -> list[Change]:
        m, md, rp = f"{SRC}/Migrations.cs", f"{SRC}/Models.cs", f"{SRC}/LinkRepository.cs"
        return [
            edit(m, '''CREATE INDEX idx_clicks_code_time ON clicks(code, clicked_at);"),
    };''', '''CREATE INDEX idx_clicks_code_time ON clicks(code, clicked_at);"),
        new Migration(2, @"
ALTER TABLE links ADD COLUMN expires_at TEXT;
ALTER TABLE clicks ADD COLUMN is_bot INTEGER NOT NULL DEFAULT 0;"),
    };'''),
            edit(md, "public sealed record CreateLinkRequest(string? Url, string? Alias);",
                 "public sealed record CreateLinkRequest(string? Url, string? Alias, int? ExpiresInSeconds = null);"),
            edit(md, "public sealed record Link(string Code, string Url, DateTimeOffset CreatedAt);",
                 "public sealed record Link(string Code, string Url, DateTimeOffset CreatedAt, DateTimeOffset? ExpiresAt = null)\n"
                 "{\n    public bool IsExpired(DateTimeOffset now) => ExpiresAt is not null && now >= ExpiresAt.Value;\n}"),
            edit(md, "string? IpHash, string? UserAgent);",
                 "string? IpHash, string? UserAgent, bool IsBot = false);"),
            edit(md, "    IReadOnlyList<ReferrerCount> TopReferrers);",
                 "    IReadOnlyList<ReferrerCount> TopReferrers,\n    int BotClicks = 0);"),
            edit(md, "    public const int MaxUrlLength = 2048;\n",
                 "    public const int MaxUrlLength = 2048;\n    public const int MinTtlSeconds = 60;\n"
                 "    public const int MaxTtlSeconds = 366 * 24 * 3600;\n"),
            edit(md, "        return errors;\n",
                 "        if (request.ExpiresInSeconds is { } ttl && (ttl < MinTtlSeconds || ttl > MaxTtlSeconds))\n"
                 "        {\n"
                 "            errors.Add(new FieldError(\n"
                 "                \"expiresInSeconds\", $\"expiresInSeconds must be between {MinTtlSeconds} and {MaxTtlSeconds}\"));\n"
                 "        }\n\n"
                 "        return errors;\n"),
            edit(rp, '''                "INSERT INTO links (code, url, created_at) VALUES ($code, $url, $created)",
                ("$code", link.Code),
                ("$url", link.Url),
                ("$created", Iso(link.CreatedAt)));''', '''                "INSERT INTO links (code, url, created_at, expires_at) VALUES ($code, $url, $created, $expires)",
                ("$code", link.Code),
                ("$url", link.Url),
                ("$created", Iso(link.CreatedAt)),
                ("$expires", link.ExpiresAt is null ? null : Iso(link.ExpiresAt.Value)));'''),
            edit(rp, '"SELECT code, url, created_at FROM links WHERE code = $code"',
                 '"SELECT code, url, created_at, expires_at FROM links WHERE code = $code"'),
            edit(rp, "return new Link(reader.GetString(0), reader.GetString(1), Parse(reader.GetString(2)));",
                 "return new Link(\n                reader.GetString(0),\n                reader.GetString(1),\n"
                 "                Parse(reader.GetString(2)),\n"
                 "                reader.IsDBNull(3) ? null : Parse(reader.GetString(3)));"),
            edit(rp, '''                "INSERT INTO clicks (code, clicked_at, referrer_host, ip_hash, user_agent) "
                + "VALUES ($code, $at, $referrer, $ip, $agent)",''', '''                "INSERT INTO clicks (code, clicked_at, referrer_host, ip_hash, user_agent, is_bot) "
                + "VALUES ($code, $at, $referrer, $ip, $agent, $bot)",'''),
            edit(rp, '                ("$agent", click.UserAgent));',
                 '                ("$agent", click.UserAgent),\n                ("$bot", click.IsBot ? 1 : 0));'),
            edit(rp, '"SELECT COUNT(*), MAX(clicked_at) FROM clicks WHERE code = $code", ("$code", code)))',
                 '"SELECT COUNT(*), MAX(clicked_at) FROM clicks WHERE code = $code AND is_bot = 0", ("$code", code)))'),
            edit(rp, '''                last = reader.IsDBNull(1) ? null : reader.GetString(1);
            }
''', '''                last = reader.IsDBNull(1) ? null : reader.GetString(1);
            }

            long bots;
            using (var command = Command(
                "SELECT COUNT(*) FROM clicks WHERE code = $code AND is_bot = 1", ("$code", code)))
            {
                bots = Convert.ToInt64(command.ExecuteScalar());
            }
'''),
            edit(rp, '+ "WHERE code = $code GROUP BY day ORDER BY day",',
                 '+ "WHERE code = $code AND is_bot = 0 GROUP BY day ORDER BY day",'),
            edit(rp, '+ "WHERE code = $code AND referrer_host IS NOT NULL "',
                 '+ "WHERE code = $code AND is_bot = 0 AND referrer_host IS NOT NULL "'),
            edit(rp, "                byDay,\n                referrers);",
                 "                byDay,\n                referrers,\n                (int)bots);"),
            Change(f"{TESTS}/StorageV2Tests.cs", STORAGE_V2_TESTS),
        ]

    def _service_rules(self) -> list[Change]:
        sv, an = f"{SRC}/LinkService.cs", f"{SRC}/Analytics.cs"
        return [
            edit(sv, "    public Link Shorten(string url, string? alias)",
                 "    public Link Shorten(string url, string? alias, int? expiresInSeconds = null)"),
            edit(sv, "        var now = _clock.UtcNow();\n",
                 "        var now = _clock.UtcNow();\n"
                 "        DateTimeOffset? expiresAt = expiresInSeconds is null ? null : now.AddSeconds(expiresInSeconds.Value);\n"),
            edit(sv, "new Link(alias, url, now)", "new Link(alias, url, now, expiresAt)"),
            edit(sv, "new Link(code, url, now)", "new Link(code, url, now, expiresAt)"),
            edit(sv, "    public Link? Resolve(string code) => _repository.Get(code);",
                 "    public bool IsExpired(Link link) => link.IsExpired(_clock.UtcNow());\n\n"
                 "    public Link? Resolve(string code) => _repository.Get(code);"),
            edit(an, "            agent);", "            agent,\n            BotDetector.IsBot(userAgent));"),
            Change(f"{TESTS}/LinkServiceExpiryTests.cs", LINK_SERVICE_EXPIRY_TESTS),
        ]

    def _http_layer(self) -> list[Change]:
        ep, oa = f"{SRC}/Endpoints.cs", "openapi.yaml"
        return [
            edit(ep, '        createdAt = link.CreatedAt.ToString("o"),\n',
                 '        createdAt = link.CreatedAt.ToString("o"),\n        expiresAt = link.ExpiresAt?.ToString("o"),\n'),
            edit(ep, "        topReferrers = stats.TopReferrers.Select(r => new { referrer = r.Referrer, clicks = r.Clicks }).ToList(),\n",
                 "        topReferrers = stats.TopReferrers.Select(r => new { referrer = r.Referrer, clicks = r.Clicks }).ToList(),\n"
                 "        botClicks = stats.BotClicks,\n"),
            edit(ep, "link = service.Shorten(url, body.Alias);", "link = service.Shorten(url, body.Alias, body.ExpiresInSeconds);"),
            edit(ep, 'app.MapGet("/{code}", RedirectToDestination);',
                 'app.MapMethods("/{code}", new[] { "GET", "HEAD" }, RedirectToDestination);'),
            edit(ep, "///   GET    /{code}                     redirect (302)",
                 "///   GET    /{code}                     redirect (302); 410 if expired; HEAD not counted"),
            edit(ep, '''        if (link is null)
        {
            return Error(404, "not_found", "link not found");
        }

        service.RecordClick(''', '''        if (link is null)
        {
            return Error(404, "not_found", "link not found");
        }

        if (service.IsExpired(link))
        {
            return Error(410, "link_expired", "this link has expired");
        }

        if (HttpMethods.IsHead(context.Request.Method))
        {
            // Preview and uptime tools probe with HEAD; that is not a visit.
            context.Response.Headers.CacheControl = "no-store";
            return Results.Redirect(link.Url);
        }

        service.RecordClick('''),
            edit(oa, '''        "302":
          description: Redirect
          headers:
            Location: { schema: { type: string } }
        "404": { $ref: "#/components/responses/Error" }''', '''        "302":
          description: Redirect (HEAD requests redirect without being counted)
          headers:
            Location: { schema: { type: string } }
        "404": { $ref: "#/components/responses/Error" }
        "410": { $ref: "#/components/responses/Error" }'''),
            edit(oa, '''        alias: { type: string, pattern: "^[A-Za-z0-9_-]{3,32}$" }
''', '''        alias: { type: string, pattern: "^[A-Za-z0-9_-]{3,32}$" }
        expiresInSeconds: { type: integer, minimum: 60, maximum: 31622400 }
'''),
            edit(oa, '''        shortUrl: { type: string }
        url: { type: string }
        createdAt: { type: string, format: date-time }
''', '''        shortUrl: { type: string }
        url: { type: string }
        createdAt: { type: string, format: date-time }
        expiresAt: { type: string, format: date-time, nullable: true }
'''),
            edit(oa, "        totalClicks: { type: integer }\n",
                 "        totalClicks: { type: integer, description: human clicks only }\n"
                 "        botClicks: { type: integer }\n"),
            Change(f"{TESTS}/ApiV2Tests.cs", API_V2_TESTS),
        ]

    def changelog(self, requirements: dict) -> str:
        return ("- Fixed: HEAD requests and link-preview bots no longer inflate totalClicks "
                "(reported separately as botClicks).\n- Schema: migration 2 (additive).")
