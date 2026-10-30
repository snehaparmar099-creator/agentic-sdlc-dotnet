using System.Net;
using System.Net.Http.Json;
using System.Text.Json;

namespace UrlShortener.Tests;

/// <summary>End-to-end flows against a file-backed database, as the service runs in production.</summary>
public class IntegrationTests
{
    private static string TempDb() => Path.Combine(Path.GetTempPath(), $"e2e-{Guid.NewGuid():N}.db");

    private static ShortenerSettings FileSettings(string dbPath) =>
        new() { BaseUrl = "https://sho.rt", DbPath = dbPath, IpHashSalt = "s" };

    private static async Task<JsonElement> Json(HttpResponseMessage response) =>
        await response.Content.ReadFromJsonAsync<JsonElement>();

    [Fact]
    public async Task Full_link_lifecycle()
    {
        using var factory = TestApp.Create(FileSettings(TempDb()));
        using var client = TestApp.Client(factory);

        var created = await client.PostAsJsonAsync("/api/v1/links", new { url = "https://example.com/launch", alias = "launch" });
        Assert.Equal(HttpStatusCode.Created, created.StatusCode);

        foreach (var referrer in new[] { "https://t.co/a", "https://t.co/b", "https://news.example/x", null })
        {
            var request = new HttpRequestMessage(HttpMethod.Get, "/launch");
            if (referrer is not null)
            {
                request.Headers.Referrer = new Uri(referrer);
            }

            Assert.Equal(HttpStatusCode.Redirect, (await client.SendAsync(request)).StatusCode);
        }

        var stats = await Json(await client.GetAsync("/api/v1/links/launch/stats"));
        Assert.Equal(4, stats.GetProperty("totalClicks").GetInt32());
        var top = stats.GetProperty("topReferrers")[0];
        Assert.Equal("t.co", top.GetProperty("referrer").GetString());
        Assert.Equal(2, top.GetProperty("clicks").GetInt32());

        Assert.Equal(HttpStatusCode.NoContent, (await client.DeleteAsync("/api/v1/links/launch")).StatusCode);
        Assert.Equal(HttpStatusCode.NotFound, (await client.GetAsync("/launch")).StatusCode);
    }

    [Fact]
    public async Task Concurrent_creates_produce_unique_codes()
    {
        using var factory = TestApp.Create(FileSettings(TempDb()));
        using var client = TestApp.Client(factory);

        var responses = await Task.WhenAll(Enumerable.Range(0, 100).Select(i =>
            client.PostAsJsonAsync("/api/v1/links", new { url = $"https://example.com/{i}" })));
        var codes = new HashSet<string>();
        foreach (var response in responses)
        {
            Assert.Equal(HttpStatusCode.Created, response.StatusCode);
            codes.Add((await Json(response)).GetProperty("code").GetString()!);
        }

        Assert.Equal(100, codes.Count);
    }

    [Fact]
    public async Task Concurrent_alias_race_has_single_winner()
    {
        using var factory = TestApp.Create(FileSettings(TempDb()));
        using var client = TestApp.Client(factory);

        var responses = await Task.WhenAll(Enumerable.Range(0, 20).Select(i =>
            client.PostAsJsonAsync("/api/v1/links", new { url = $"https://owner{i}.example", alias = "hot" })));
        Assert.Equal(1, responses.Count(r => r.StatusCode == HttpStatusCode.Created));
        Assert.Equal(19, responses.Count(r => r.StatusCode == HttpStatusCode.Conflict));
    }

    [Fact]
    public async Task Data_survives_app_restart()
    {
        var dbPath = TempDb();
        string code;
        using (var factory = TestApp.Create(FileSettings(dbPath)))
        using (var client = TestApp.Client(factory))
        {
            var created = await client.PostAsJsonAsync("/api/v1/links", new { url = "https://example.com" });
            code = (await Json(created)).GetProperty("code").GetString()!;
        }

        using var restarted = TestApp.Create(FileSettings(dbPath));
        using var again = TestApp.Client(restarted);
        Assert.Equal(HttpStatusCode.Redirect, (await again.GetAsync($"/{code}")).StatusCode);
    }
}
