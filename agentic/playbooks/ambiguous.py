"""Ambiguous: "make the shortener safer for public use and protect it from abuse".

"Safer" and "abuse" are not specifications. The requirements agent turns them
into multiple-choice questions; the human's answers (and later amendments)
determine the design, the threat model and the task graph. Choices the offline
playbook cannot implement are recorded as explicit fallbacks, never silently
ignored.
"""

from __future__ import annotations

import re

from ..workspace import Change
from .base import Playbook
from .brownfield import edit

Q1_OPTIONS = [
    "Automated bulk link creation (spam bots hammering the create API)",
    "Short links pointing at internal/private network addresses (internal phishing, SSRF-style targets)",
    "Both of the above",
    "Malware/phishing destinations (needs a third-party threat-intelligence feed)",
]
Q2_OPTIONS = [
    "Per client IP, in-process token bucket (single instance today)",
    "Per client IP, shared Redis store (multi-instance)",
    "Per API key (requires introducing authentication)",
]
Q3_OPTIONS = ["20 creates/minute, burst 10", "60 creates/minute, burst 20", "5 creates/minute, burst 5"]

SELF_LINK_HINTS = ("short link", "own domain", "our domain", "loop", "chain", "itself")


def _answers(requirements: dict) -> dict[str, str]:
    return {k: v["answer"] for k, v in (requirements.get("clarifications") or {}).items()}


def _scope(requirements: dict) -> tuple[bool, bool]:
    q1 = _answers(requirements).get("Q1", Q1_OPTIONS[2])
    rate = q1 in (Q1_OPTIONS[0], Q1_OPTIONS[2]) or "bulk" in q1.lower() or "rate" in q1.lower()
    dest = q1 in (Q1_OPTIONS[1], Q1_OPTIONS[2]) or "internal" in q1.lower() or "private" in q1.lower()
    if not (rate or dest):  # e.g. threat-intel option: not implementable offline -> recommended scope
        rate = dest = True
    return rate, dest


def _limits(requirements: dict) -> tuple[int, int]:
    nums = [int(n) for n in re.findall(r"\d+", _answers(requirements).get("Q3", Q3_OPTIONS[0]))]
    return (nums[0], nums[1]) if len(nums) >= 2 else (20, 10)


def _self_block(requirements: dict) -> bool:
    return any(r["id"] == "FR-3" for r in requirements.get("functional", []))


SRC = "src/UrlShortener"
TESTS = "tests/UrlShortener.Tests"

RATE_LIMITER = '''using System.Diagnostics;

namespace UrlShortener;

/// <summary>
/// Per-client token-bucket rate limiting. Each client key gets a bucket of `burst` tokens
/// refilled at `ratePerMinute`. The key table is bounded (least recently used keys are evicted)
/// so a flood of distinct keys cannot exhaust memory. State is per process: correct for a
/// single instance; a shared store is needed once the service runs on several instances.
/// </summary>
public sealed class TokenBucketLimiter
{
    private sealed class Bucket
    {
        public string Key = "";
        public double Tokens;
        public double Last;
    }

    private readonly double _ratePerSecond;
    private readonly double _burst;
    private readonly Func<double> _clock;
    private readonly int _maxKeys;
    private readonly Dictionary<string, LinkedListNode<Bucket>> _buckets = new();
    private readonly LinkedList<Bucket> _order = new();
    private readonly object _gate = new();

    public TokenBucketLimiter(int ratePerMinute, int burst, Func<double>? clock = null, int maxKeys = 10_000)
    {
        if (ratePerMinute <= 0 || burst <= 0)
        {
            throw new ArgumentOutOfRangeException(nameof(ratePerMinute), "rate and burst must be positive");
        }

        _ratePerSecond = ratePerMinute / 60.0;
        _burst = burst;
        _clock = clock ?? MonotonicSeconds;
        _maxKeys = maxKeys;
    }

    public int Count
    {
        get
        {
            lock (_gate)
            {
                return _buckets.Count;
            }
        }
    }

    /// <summary>Consume one token for the key. Returns whether it was allowed and, if not, when to retry.</summary>
    public (bool Allowed, int RetryAfterSeconds) Check(string key)
    {
        var now = _clock();
        lock (_gate)
        {
            if (_buckets.TryGetValue(key, out var node))
            {
                _order.Remove(node);
            }
            else
            {
                node = new LinkedListNode<Bucket>(new Bucket { Key = key, Tokens = _burst, Last = now });
                _buckets[key] = node;
            }

            _order.AddLast(node);
            var bucket = node.Value;
            bucket.Tokens = Math.Min(_burst, bucket.Tokens + ((now - bucket.Last) * _ratePerSecond));
            bucket.Last = now;
            var allowed = bucket.Tokens >= 1.0;
            if (allowed)
            {
                bucket.Tokens -= 1.0;
            }

            while (_buckets.Count > _maxKeys)
            {
                var oldest = _order.First!;
                _order.RemoveFirst();
                _buckets.Remove(oldest.Value.Key);
            }

            var retryAfter = allowed ? 0 : Math.Max(1, (int)Math.Ceiling((1.0 - bucket.Tokens) / _ratePerSecond));
            return (allowed, retryAfter);
        }
    }

    private static double MonotonicSeconds() => Stopwatch.GetTimestamp() / (double)Stopwatch.Frequency;
}
'''

REDIS_ADDON = '''
/// <summary>Shared limiter for multi-instance deployments (fixed one-minute windows in Redis).</summary>
public sealed class RedisFixedWindowLimiter
{
    private readonly StackExchange.Redis.IDatabase _db;
    private readonly int _ratePerMinute;

    public RedisFixedWindowLimiter(string connectionString, int ratePerMinute)
    {
        _db = StackExchange.Redis.ConnectionMultiplexer.Connect(connectionString).GetDatabase();
        _ratePerMinute = ratePerMinute;
    }

    public (bool Allowed, int RetryAfterSeconds) Check(string key)
    {
        var now = DateTimeOffset.UtcNow.ToUnixTimeSeconds();
        var counter = $"rl:{key}:{now / 60}";
        var count = _db.StringIncrement(counter);
        if (count == 1)
        {
            _db.KeyExpire(counter, TimeSpan.FromMinutes(2));
        }

        return count <= _ratePerMinute ? (true, 0) : (false, 60 - (int)(now % 60));
    }
}
'''

RATE_LIMITER_TESTS = '''namespace UrlShortener.Tests;

public class RateLimiterTests
{
    private sealed class ManualClock
    {
        public double T = 1000;

        public double Now() => T;
    }

    [Fact]
    public void Burst_then_limited_with_retry_after()
    {
        var clock = new ManualClock();
        var limiter = new TokenBucketLimiter(60, 3, clock.Now);
        Assert.True(limiter.Check("a").Allowed);
        Assert.True(limiter.Check("a").Allowed);
        Assert.True(limiter.Check("a").Allowed);
        var (allowed, retryAfter) = limiter.Check("a");
        Assert.False(allowed);
        Assert.Equal(1, retryAfter);
    }

    [Fact]
    public void Tokens_refill_over_time()
    {
        var clock = new ManualClock();
        var limiter = new TokenBucketLimiter(60, 1, clock.Now);
        Assert.True(limiter.Check("a").Allowed);
        Assert.False(limiter.Check("a").Allowed);
        clock.T += 1.0;
        Assert.True(limiter.Check("a").Allowed);
    }

    [Fact]
    public void Clients_are_isolated()
    {
        var limiter = new TokenBucketLimiter(60, 1, new ManualClock().Now);
        Assert.True(limiter.Check("a").Allowed);
        Assert.True(limiter.Check("b").Allowed);
        Assert.False(limiter.Check("a").Allowed);
    }

    [Fact]
    public void Key_table_is_bounded()
    {
        var limiter = new TokenBucketLimiter(60, 1, new ManualClock().Now, maxKeys: 100);
        for (var i = 0; i < 1000; i++)
        {
            limiter.Check($"client-{i}");
        }

        Assert.Equal(100, limiter.Count);
    }

    [Fact]
    public void Thread_safety_never_exceeds_burst()
    {
        var limiter = new TokenBucketLimiter(1, 50, new ManualClock().Now);
        var allowed = 0;
        Parallel.For(0, 200, _ =>
        {
            if (limiter.Check("shared").Allowed)
            {
                Interlocked.Increment(ref allowed);
            }
        });
        Assert.Equal(50, allowed);
    }
}
'''


def url_safety_class(self_block: bool) -> str:
    own = '''
        if (ownHosts is not null && ownHosts.Contains(host))
        {
            return "destination is already a short link on this service (redirect chains are not allowed)";
        }
''' if self_block else ""
    return '''using System.Globalization;
using System.Net;
using System.Net.Sockets;

namespace UrlShortener;

/// <summary>
/// Destination safety checks for new short links. Blocks destinations that point into private
/// networks or at internal-only hostnames, including obfuscated IP literals (decimal, hex,
/// IPv4-mapped IPv6), so the shortener cannot dress up internal targets as public links.
/// No DNS resolution happens at create time: it would add latency and false assurance (DNS can
/// change after the check). The redirect itself is followed by the visitor's browser.
/// </summary>
public static class UrlSafety
{
    private static readonly HashSet<string> BlockedHostnames = new(StringComparer.OrdinalIgnoreCase)
    {
        "localhost", "localhost.localdomain", "metadata.google.internal",
    };

    private static readonly string[] BlockedSuffixes =
    {
        ".localhost", ".local", ".internal", ".lan", ".home.arpa", ".intranet", ".corp",
    };

    /// <summary>Returns a reason the destination is not allowed, or null if it is acceptable.</summary>
    public static string? CheckDestination(string url, IReadOnlySet<string>? ownHosts = null)
    {
        if (!Uri.TryCreate(url, UriKind.Absolute, out var uri) || string.IsNullOrEmpty(uri.Host))
        {
            return "destination has no host";
        }

        var host = uri.Host.Trim('[', ']').TrimEnd('.').ToLowerInvariant();
        if (BlockedHostnames.Contains(host) || BlockedSuffixes.Any(s => host.EndsWith(s, StringComparison.Ordinal)))
        {
            return "destination is an internal hostname";
        }

        var ip = ParseIp(host);
        if (ip is not null)
        {
            return IsPublic(ip) ? null : "destination is a private, loopback or reserved network address";
        }

        if (!host.Contains('.'))
        {
            return "destination must be a fully qualified public hostname";
        }
''' + own + '''
        return null;
    }

    private static IPAddress? ParseIp(string host)
    {
        if (host.StartsWith("0x", StringComparison.Ordinal)
            && uint.TryParse(host[2..], NumberStyles.HexNumber, CultureInfo.InvariantCulture, out var hex))
        {
            return FromUInt32(hex);
        }

        if (host.Length > 0 && host.All(char.IsAsciiDigit)
            && uint.TryParse(host, NumberStyles.None, CultureInfo.InvariantCulture, out var dec))
        {
            return FromUInt32(dec);
        }

        return IPAddress.TryParse(host, out var ip) && (host.Contains(':') || host.Count(c => c == '.') == 3)
            ? ip
            : null;
    }

    private static IPAddress FromUInt32(uint value) =>
        new(new[] { (byte)(value >> 24), (byte)(value >> 16), (byte)(value >> 8), (byte)value });

    private static bool IsPublic(IPAddress ip)
    {
        if (ip.IsIPv4MappedToIPv6)
        {
            ip = ip.MapToIPv4();
        }

        if (IPAddress.IsLoopback(ip))
        {
            return false;
        }

        if (ip.AddressFamily == AddressFamily.InterNetwork)
        {
            var b = ip.GetAddressBytes();
            var reserved = b[0] == 0 || b[0] == 10 || b[0] == 127 || b[0] >= 224
                || (b[0] == 100 && b[1] >= 64 && b[1] <= 127)
                || (b[0] == 169 && b[1] == 254)
                || (b[0] == 172 && b[1] >= 16 && b[1] <= 31)
                || (b[0] == 192 && b[1] == 0 && b[2] == 0)
                || (b[0] == 192 && b[1] == 168)
                || (b[0] == 198 && (b[1] == 18 || b[1] == 19));
            return !reserved;
        }

        if (ip.AddressFamily == AddressFamily.InterNetworkV6)
        {
            if (ip.Equals(IPAddress.IPv6Any))
            {
                return false;
            }

            return !(ip.IsIPv6LinkLocal || ip.IsIPv6SiteLocal || ip.IsIPv6UniqueLocal || ip.IsIPv6Multicast);
        }

        return false;
    }
}
'''


def url_safety_tests(self_block: bool) -> str:
    extra = '''
    [Fact]
    public void Blocks_own_short_domain()
    {
        var reason = UrlSafety.CheckDestination("https://sho.rt/abc123", new HashSet<string> { "sho.rt" });
        Assert.NotNull(reason);
        Assert.Contains("short link", reason);
    }
''' if self_block else ""
    return '''namespace UrlShortener.Tests;

public class UrlSafetyTests
{
    [Theory]
    [InlineData("http://127.0.0.1/admin")]
    [InlineData("http://10.0.0.5")]
    [InlineData("http://192.168.1.1/router")]
    [InlineData("http://172.16.0.9")]
    [InlineData("http://169.254.169.254/latest/meta-data")]
    [InlineData("http://[::1]/")]
    [InlineData("http://[fd00::1]/")]
    [InlineData("http://0.0.0.0/")]
    [InlineData("http://100.64.0.1/")]
    public void Blocks_private_and_reserved_addresses(string url)
    {
        Assert.NotNull(UrlSafety.CheckDestination(url));
    }

    [Theory]
    [InlineData("http://2130706433/")]
    [InlineData("http://0x7f000001/")]
    [InlineData("http://[::ffff:127.0.0.1]/")]
    public void Blocks_obfuscated_ip_literals(string url)
    {
        Assert.NotNull(UrlSafety.CheckDestination(url));
    }

    [Theory]
    [InlineData("http://localhost:8080")]
    [InlineData("https://grafana.internal")]
    [InlineData("http://printer.local")]
    [InlineData("http://intranet/")]
    [InlineData("http://metadata.google.internal/computeMetadata/v1/")]
    public void Blocks_internal_hostnames(string url)
    {
        Assert.NotNull(UrlSafety.CheckDestination(url));
    }

    [Theory]
    [InlineData("https://example.com/path")]
    [InlineData("https://sub.example.co.uk:8443/x")]
    [InlineData("http://93.184.216.34/")]
    [InlineData("https://[2606:4700:4700::1111]/")]
    public void Allows_public_destinations(string url)
    {
        Assert.Null(UrlSafety.CheckDestination(url));
    }
''' + extra + '''}
'''


def api_abuse_tests(rate: bool, dest: bool, self_block: bool) -> str:
    settings = ('''    private static ShortenerSettings Settings(int perMinute = 60, int burst = 1000) =>
        TestSettings.InMemory() with { RateLimitPerMinute = perMinute, RateLimitBurst = burst };
''' if rate else '''    private static ShortenerSettings Settings() => TestSettings.InMemory();
''')
    parts = ['''using System.Net;
using System.Net.Http.Json;
using System.Text.Json;

namespace UrlShortener.Tests;

public class ApiAbuseTests
{
''' + settings + '''
    private static async Task<JsonElement> Json(HttpResponseMessage response) =>
        await response.Content.ReadFromJsonAsync<JsonElement>();
''']
    if rate:
        parts.append('''
    [Fact]
    public async Task Create_is_rate_limited_per_client()
    {
        using var factory = TestApp.Create(Settings(perMinute: 60, burst: 3));
        using var client = TestApp.Client(factory);
        var statuses = new List<HttpStatusCode>();
        for (var i = 0; i < 4; i++)
        {
            statuses.Add((await client.PostAsJsonAsync("/api/v1/links", new { url = "https://example.com" })).StatusCode);
        }

        Assert.Equal(new[] { HttpStatusCode.Created, HttpStatusCode.Created, HttpStatusCode.Created, HttpStatusCode.TooManyRequests }, statuses);
        var limited = await client.PostAsJsonAsync("/api/v1/links", new { url = "https://example.com" });
        Assert.Equal("rate_limited", (await Json(limited)).GetProperty("error").GetProperty("code").GetString());
        Assert.True(int.Parse(limited.Headers.GetValues("Retry-After").Single()) >= 1);
    }

    [Fact]
    public async Task Redirects_are_not_rate_limited()
    {
        using var factory = TestApp.Create(Settings(perMinute: 60, burst: 1));
        using var client = TestApp.Client(factory);
        var code = (await Json(await client.PostAsJsonAsync("/api/v1/links", new { url = "https://example.com" })))
            .GetProperty("code").GetString();
        for (var i = 0; i < 20; i++)
        {
            Assert.Equal(HttpStatusCode.Redirect, (await client.GetAsync($"/{code}")).StatusCode);
        }
    }
''')
    if dest:
        parts.append('''
    [Theory]
    [InlineData("http://169.254.169.254/latest/meta-data")]
    [InlineData("http://localhost:6379")]
    public async Task Internal_destination_rejected(string url)
    {
        using var factory = TestApp.Create(Settings());
        using var client = TestApp.Client(factory);
        var response = await client.PostAsJsonAsync("/api/v1/links", new { url });
        Assert.Equal(HttpStatusCode.UnprocessableEntity, response.StatusCode);
        Assert.Equal("unsafe_destination", (await Json(response)).GetProperty("error").GetProperty("code").GetString());
    }

    [Fact]
    public async Task Public_destination_still_accepted()
    {
        using var factory = TestApp.Create(Settings());
        using var client = TestApp.Client(factory);
        var response = await client.PostAsJsonAsync("/api/v1/links", new { url = "https://example.com" });
        Assert.Equal(HttpStatusCode.Created, response.StatusCode);
    }
''')
    if self_block:
        parts.append('''
    [Fact]
    public async Task Link_to_own_short_domain_rejected()
    {
        using var factory = TestApp.Create(Settings());
        using var client = TestApp.Client(factory);
        var response = await client.PostAsJsonAsync("/api/v1/links", new { url = "https://sho.rt/abc1234" });
        Assert.Equal(HttpStatusCode.UnprocessableEntity, response.StatusCode);
    }
''')
    parts.append("}\n")
    return "".join(parts)


class AmbiguousPlaybook(Playbook):
    name = "ambiguous"

    def requirements(self, text: str, feedback: list[str]) -> dict:
        def r(i, t, *acc):
            return {"id": i, "text": t, "acceptance": list(acc)}

        functional = [
            r("FR-1", "Limit how fast a single client can create links",
              "creates beyond the limit return 429 with Retry-After", "redirects are never rate limited"),
            r("FR-2", "Reject destinations on private networks or internal hostnames",
              "loopback/private/link-local/reserved IPs (incl. obfuscated forms) and internal hostnames "
              "return 422 unsafe_destination", "public destinations are unaffected"),
        ]
        amendments = " ".join(feedback).lower()
        if any(h in amendments for h in SELF_LINK_HINTS):
            functional.append(r("FR-3", "Reject destinations that are short links on this service",
                                "a URL on the service's own host returns 422 (prevents redirect chains)"))
        return {
            "problem_statement": "Reduce abuse of the public URL shortener without degrading legitimate use. "
                                 "'Safer' and 'abuse' must be pinned down before design.",
            "functional": functional,
            "non_functional": [
                r("NFR-1", "No new infrastructure unless explicitly approved",
                  "no new runtime dependency without change-control approval"),
                r("NFR-2", "Existing behaviour preserved", "existing test suite passes"),
            ],
            "out_of_scope": ["malware/phishing reputation checks (needs a vendor feed)", "user accounts"],
            "assumptions": ["the service runs as a single instance today",
                            "client identity = connection IP (deploy behind a trusted proxy with "
                            "uvicorn --proxy-headers so this is the real client IP)"],
            "ambiguities": [
                {"term": "abuse", "why": "could mean spam creation volume, malicious destinations, or both"},
                {"term": "safer", "why": "no measurable target (limits, blocked destinations) was given"},
            ],
            "questions": [
                {"id": "Q1", "text": "Which abuse should this change address?", "options": Q1_OPTIONS,
                 "recommended": 2, "rationale": "Both are cheap to mitigate in-process; threat intel needs a vendor."},
                {"id": "Q2", "text": "How should clients be identified and limited?", "options": Q2_OPTIONS,
                 "recommended": 0, "rationale": "Single instance today; Redis/auth add infrastructure."},
                {"id": "Q3", "text": "What creation limit per client?", "options": Q3_OPTIONS,
                 "recommended": 0, "rationale": "Above any human pace, far below a spam script."},
            ],
            "keywords": ["create", "url", "link", "validate", "config", "destination"],
        }

    def design(self, requirements: dict, analysis: dict | None, feedback: list[str]) -> dict:
        rate, dest = _scope(requirements)
        per_min, burst = _limits(requirements)
        answers = _answers(requirements)
        notes = []
        if answers.get("Q2") and answers["Q2"] != Q2_OPTIONS[0]:
            notes.append(f"Requested limiter '{answers['Q2']}' is not available offline; using the in-process "
                         "limiter behind the same check() interface so it can be swapped later.")
        if answers.get("Q1") == Q1_OPTIONS[3]:
            notes.append("Threat-intel scanning needs a vendor feed; implemented the recommended scope instead.")
        comps, adrs, trace = [], [], {}
        if rate:
            comps.append({"name": "RateLimiter", "module": "RateLimiter.cs",
                          "responsibility": f"token bucket per client IP ({per_min}/min, burst {burst})"})
            adrs.append({"id": "ADR-13", "title": "In-process token bucket on link creation only",
                         "decision": f"Token bucket per client IP, {per_min}/min burst {burst}, applied to "
                                     "POST /api/v1/links; 429 + Retry-After",
                         "rationale": "Creation is the abuse vector; redirects must stay unthrottled for viral "
                                      "links. In-process is correct for one instance and adds no infrastructure.",
                         "alternatives": ["Redis-backed limiter (new infra)", "Limit redirects too (hurts users)"]})
            trace["FR-1"] = ["RateLimiter", "HTTP API"]
        if dest:
            comps.append({"name": "DestinationPolicy", "module": "UrlSafety.cs",
                          "responsibility": "reject private/internal destinations"})
            adrs.append({"id": "ADR-14", "title": "Static destination checks without DNS",
                         "decision": "Reject IP literals that are not globally routable (incl. obfuscated forms) "
                                     "and internal hostname suffixes; no DNS lookups at create time",
                         "rationale": "Deterministic and fast; DNS answers can change after the check so "
                                      "resolution would give false assurance.",
                         "alternatives": ["Resolve and check IPs (latency, rebinding)", "Allow-list domains"]})
            trace["FR-2"] = ["DestinationPolicy", "HTTP API"]
        if _self_block(requirements):
            adrs.append({"id": "ADR-15", "title": "No redirect chains through this service",
                         "decision": "Reject destinations whose host is this service's own host",
                         "rationale": "Chains hide the final destination from users and from our checks.",
                         "alternatives": ["Resolve chains at create time"]})
            trace["FR-3"] = ["DestinationPolicy"]
        descoped = {}
        if not rate:
            descoped["FR-1"] = f"descoped by clarification Q1 ('{answers.get('Q1')}')"
        if not dest:
            descoped["FR-2"] = f"descoped by clarification Q1 ('{answers.get('Q1')}')"
            if _self_block(requirements):
                descoped["FR-3"] = "depends on the descoped destination policy"
        comps.append({"name": "HTTP API", "module": "Endpoints.cs", "responsibility": "enforce the controls"})
        return {
            "summary": "Add abuse controls at the edge of the create endpoint. " + " ".join(notes),
            "components": comps, "adrs": adrs,
            "api": [{"method": "POST", "path": "/api/v1/links", "purpose": "429 rate_limited / 422 unsafe_destination"}],
            "data_model": ["no schema change"],
            "traceability": trace,
            "descoped": descoped,
            "scope": {"rate_limit": rate, "destination_policy": dest, "limits": [per_min, burst],
                      "self_link_block": _self_block(requirements)},
        }

    def threats(self, requirements: dict) -> list[dict]:
        rate, dest = _scope(requirements)
        risks = []
        if rate:
            risks += [
                {"id": "R1", "category": "Denial of service", "risk": "Scripted bulk creation of spam links",
                 "likelihood": "high", "impact": "high", "mitigation": "per-client token bucket",
                 "verified_by": [f"{TESTS}/ApiAbuseTests.cs::Create_is_rate_limited_per_client"]},
                {"id": "R4", "category": "Denial of service", "risk": "Limiter memory exhausted by many keys",
                 "likelihood": "medium", "impact": "medium", "mitigation": "LRU-bounded key table",
                 "verified_by": [f"{TESTS}/RateLimiterTests.cs::Key_table_is_bounded"]},
                {"id": "R6", "category": "Availability", "risk": "Legitimate viral traffic throttled",
                 "likelihood": "medium", "impact": "high", "mitigation": "limit creation only, never redirects",
                 "verified_by": [f"{TESTS}/ApiAbuseTests.cs::Redirects_are_not_rate_limited"]},
            ]
        if dest:
            risks += [
                {"id": "R2", "category": "Spoofing", "risk": "Short links disguise internal targets (metadata "
                 "endpoints, admin panels)", "likelihood": "medium", "impact": "high",
                 "mitigation": "reject non-global IPs and internal hostnames",
                 "verified_by": [f"{TESTS}/UrlSafetyTests.cs::Blocks_private_and_reserved_addresses",
                                 f"{TESTS}/ApiAbuseTests.cs::Internal_destination_rejected"]},
                {"id": "R3", "category": "Spoofing", "risk": "Obfuscated IP encodings bypass the check",
                 "likelihood": "medium", "impact": "high", "mitigation": "integer and IPv4-mapped forms decoded",
                 "verified_by": [f"{TESTS}/UrlSafetyTests.cs::Blocks_obfuscated_ip_literals"]},
                {"id": "R7", "category": "Spoofing", "risk": "Public hostname resolving to a private IP",
                 "likelihood": "low", "impact": "medium", "mitigation": "accepted: no DNS at create time "
                 "(documented trade-off)", "verified_by": []},
            ]
        if _self_block(requirements):
            risks.append({"id": "R5", "category": "Spoofing", "risk": "Redirect chains through our own links",
                          "likelihood": "medium", "impact": "high", "mitigation": "reject own host",
                          "verified_by": [f"{TESTS}/UrlSafetyTests.cs::Blocks_own_short_domain"]})
        return risks

    def plan(self, requirements: dict, design: dict, analysis: dict | None) -> list[dict]:
        rate, dest = _scope(requirements)
        tasks, deps = [], []
        if rate:
            tasks.append({"id": "rate_limiter", "title": "Token-bucket rate limiter", "deps": [],
                          "files": [f"{SRC}/RateLimiter.cs", f"{SRC}/UrlShortener.csproj"],
                          "tests": [f"{TESTS}/RateLimiterTests.cs"], "requirements": ["FR-1"],
                          "description": "Thread-safe, bounded, clock-injectable limiter.", "risk": "medium"})
            deps.append("rate_limiter")
        if dest:
            reqs = ["FR-2"] + (["FR-3"] if _self_block(requirements) else [])
            tasks.append({"id": "url_safety", "title": "Destination safety policy", "deps": [],
                          "files": [f"{SRC}/UrlSafety.cs"], "tests": [f"{TESTS}/UrlSafetyTests.cs"],
                          "requirements": reqs, "self_link_block": _self_block(requirements),
                          "description": "Reject private/reserved/internal destinations.", "risk": "high"})
            deps.append("url_safety")
        per_min, burst = _limits(requirements)
        tasks.append({"id": "api_integration", "title": "Enforce controls in the create endpoint", "deps": deps,
                      "files": [f"{SRC}/Endpoints.cs", f"{SRC}/Settings.cs", f"{SRC}/Program.cs", "openapi.yaml",
                                f"{TESTS}/IntegrationTests.cs"],
                      "tests": [f"{TESTS}/ApiAbuseTests.cs"],
                      "requirements": [r["id"] for r in requirements.get("functional", [])
                                       if r["id"] not in (design.get("descoped") or {})] + ["NFR-2"],
                      "limits": [per_min, burst], "rate": rate, "dest": dest,
                      "self_link_block": _self_block(requirements),
                      "description": "Wire limiter and destination policy into POST /api/v1/links; configure "
                                     "via env; existing load-style integration tests opt out of limits explicitly.",
                      "risk": "high"})
        return tasks

    def implement(self, task: dict, feedback: list[str]) -> list[Change]:
        tid = task["id"]
        if tid == "rate_limiter":
            if not feedback:
                # First proposal over-reaches: a Redis limiter "for later" plus a new NuGet package.
                # Change control holds it; the reviewer's feedback scopes it back down.
                return [Change(f"{SRC}/RateLimiter.cs", RATE_LIMITER + REDIS_ADDON),
                        edit(f"{SRC}/UrlShortener.csproj",
                             '    <PackageReference Include="Microsoft.Data.Sqlite" Version="10.0.0" />\n',
                             '    <PackageReference Include="Microsoft.Data.Sqlite" Version="10.0.0" />\n'
                             '    <PackageReference Include="StackExchange.Redis" Version="2.8.16" />\n'),
                        Change(f"{TESTS}/RateLimiterTests.cs", RATE_LIMITER_TESTS)]
            return [Change(f"{SRC}/RateLimiter.cs", RATE_LIMITER),
                    Change(f"{TESTS}/RateLimiterTests.cs", RATE_LIMITER_TESTS)]
        if tid == "url_safety":
            sb = bool(task.get("self_link_block"))
            return [Change(f"{SRC}/UrlSafety.cs", url_safety_class(sb)),
                    Change(f"{TESTS}/UrlSafetyTests.cs", url_safety_tests(sb))]
        if tid == "api_integration":
            return self._api(task)
        raise KeyError(tid)

    def _api(self, task: dict) -> list[Change]:
        per_min, burst = task.get("limits", [20, 10])
        rate, dest, sb = task.get("rate", True), task.get("dest", True), task.get("self_link_block", False)
        cfg, prog, ep = f"{SRC}/Settings.cs", f"{SRC}/Program.cs", f"{SRC}/Endpoints.cs"
        changes: list[Change] = []
        if rate:
            changes += [
                edit(cfg, '    public string IpHashSalt { get; init; } = "dev-only-salt-change-me";\n',
                     '    public string IpHashSalt { get; init; } = "dev-only-salt-change-me";\n\n'
                     '    // Abuse controls for link creation (per client IP).\n'
                     f'    public int RateLimitPerMinute {{ get; init; }} = {per_min};\n'
                     f'    public int RateLimitBurst {{ get; init; }} = {burst};\n'),
                edit(cfg, '            IpHashSalt = Environment.GetEnvironmentVariable("SHORTENER_IP_HASH_SALT") ?? defaults.IpHashSalt,\n',
                     '            IpHashSalt = Environment.GetEnvironmentVariable("SHORTENER_IP_HASH_SALT") ?? defaults.IpHashSalt,\n'
                     '            RateLimitPerMinute = IntFromEnv("SHORTENER_RATE_LIMIT_PER_MINUTE", defaults.RateLimitPerMinute),\n'
                     '            RateLimitBurst = IntFromEnv("SHORTENER_RATE_LIMIT_BURST", defaults.RateLimitBurst),\n'),
                edit(prog, "    sp.GetRequiredService<IClock>()));\n",
                     "    sp.GetRequiredService<IClock>()));\n"
                     "builder.Services.AddSingleton(sp =>\n"
                     "{\n"
                     "    var settings = sp.GetRequiredService<ShortenerSettings>();\n"
                     "    return new TokenBucketLimiter(settings.RateLimitPerMinute, settings.RateLimitBurst);\n"
                     "});\n"),
                edit(ep, "CreateLink(HttpRequest request, LinkService service, ShortenerSettings settings)",
                     "CreateLink(\n        HttpRequest request, LinkService service, ShortenerSettings settings, TokenBucketLimiter limiter)"),
                edit(ep, "    {\n        if (request.ContentLength > MaxBodyBytes)\n",
                     "    {\n"
                     "        var clientKey = request.HttpContext.Connection.RemoteIpAddress?.ToString() ?? \"unknown\";\n"
                     "        var (allowed, retryAfter) = limiter.Check(clientKey);\n"
                     "        if (!allowed)\n"
                     "        {\n"
                     "            request.HttpContext.Response.Headers.RetryAfter = retryAfter.ToString();\n"
                     "            return Error(429, \"rate_limited\", \"too many links created; slow down\");\n"
                     "        }\n\n"
                     "        if (request.ContentLength > MaxBodyBytes)\n"),
                # Load-style integration tests opt out of abuse limits explicitly; limits have their own tests.
                edit(f"{TESTS}/IntegrationTests.cs",
                     'new() { BaseUrl = "https://sho.rt", DbPath = dbPath, IpHashSalt = "s" };',
                     'new()\n        {\n            BaseUrl = "https://sho.rt",\n            DbPath = dbPath,\n'
                     '            IpHashSalt = "s",\n'
                     '            // Load-style tests opt out of abuse limits; ApiAbuseTests covers the limits.\n'
                     '            RateLimitPerMinute = 100_000,\n            RateLimitBurst = 100_000,\n        };'),
                edit("openapi.yaml", '        "409": { $ref: "#/components/responses/Error" }\n',
                     '        "409": { $ref: "#/components/responses/Error" }\n'
                     '        "429":\n          description: Rate limited (see Retry-After)\n'
                     '          headers:\n            Retry-After: { schema: { type: integer } }\n'),
            ]
        if dest:
            check = "UrlSafety.CheckDestination(url, OwnHosts(settings))" if sb else "UrlSafety.CheckDestination(url)"
            changes.append(edit(ep, "        Link link;\n",
                                f"        var problem = {check};\n"
                                "        if (problem is not null)\n"
                                "        {\n"
                                "            return Error(422, \"unsafe_destination\", problem);\n"
                                "        }\n\n"
                                "        Link link;\n"))
            if sb:
                changes.append(edit(ep, "    public static IResult Health() =>",
                                    "    private static IReadOnlySet<string> OwnHosts(ShortenerSettings settings) =>\n"
                                    "        Uri.TryCreate(settings.BaseUrl, UriKind.Absolute, out var baseUri)\n"
                                    "            ? new HashSet<string>(StringComparer.OrdinalIgnoreCase) { baseUri.Host }\n"
                                    "            : new HashSet<string>(StringComparer.OrdinalIgnoreCase);\n\n"
                                    "    public static IResult Health() =>"))
        changes.append(Change(f"{TESTS}/ApiAbuseTests.cs", api_abuse_tests(rate, dest, sb)))
        return changes

    def changelog(self, requirements: dict) -> str:
        return "- Security: abuse controls on link creation (see ADRs)."
