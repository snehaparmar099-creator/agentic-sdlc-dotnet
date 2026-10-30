using System.Net;
using System.Net.Http.Json;
using System.Text;
using System.Text.Json;

namespace UrlShortener.Tests;

public class ApiTests : IDisposable
{
    private readonly Microsoft.AspNetCore.Mvc.Testing.WebApplicationFactory<Program> _factory;
    private readonly HttpClient _client;

    public ApiTests()
    {
        _factory = TestApp.Create(TestSettings.InMemory());
        _client = TestApp.Client(_factory);
    }

    public void Dispose()
    {
        _client.Dispose();
        _factory.Dispose();
    }

    private Task<HttpResponseMessage> Create(object body) => _client.PostAsJsonAsync("/api/v1/links", body);

    private static async Task<JsonElement> Json(HttpResponseMessage response) =>
        await response.Content.ReadFromJsonAsync<JsonElement>();

    [Fact]
    public async Task Healthz()
    {
        var response = await _client.GetAsync("/healthz");
        Assert.Equal(HttpStatusCode.OK, response.StatusCode);
        Assert.Equal("ok", (await Json(response)).GetProperty("status").GetString());
    }

    [Fact]
    public async Task Create_returns_201_with_short_url()
    {
        var response = await Create(new { url = "https://example.com/page" });
        Assert.Equal(HttpStatusCode.Created, response.StatusCode);
        var body = await Json(response);
        Assert.Equal("https://example.com/page", body.GetProperty("url").GetString());
        var code = body.GetProperty("code").GetString();
        Assert.Equal($"https://sho.rt/{code}", body.GetProperty("shortUrl").GetString());
    }

    [Fact]
    public async Task Create_with_alias_and_conflict()
    {
        Assert.Equal(HttpStatusCode.Created, (await Create(new { url = "https://a.example", alias = "team" })).StatusCode);
        var conflict = await Create(new { url = "https://b.example", alias = "team" });
        Assert.Equal(HttpStatusCode.Conflict, conflict.StatusCode);
        Assert.Equal("alias_conflict", (await Json(conflict)).GetProperty("error").GetProperty("code").GetString());
        var meta = await Json(await _client.GetAsync("/api/v1/links/team"));
        Assert.Equal("https://a.example", meta.GetProperty("url").GetString());
    }

    [Theory]
    [InlineData("{\"url\":\"javascript:alert(1)\"}")]
    [InlineData("{\"url\":\"https://ok.example\",\"alias\":\"a b\"}")]
    [InlineData("{}")]
    [InlineData("{\"alias\":\"abc\"}")]
    public async Task Create_validation_errors(string json)
    {
        var response = await _client.PostAsync("/api/v1/links", new StringContent(json, Encoding.UTF8, "application/json"));
        Assert.Equal(HttpStatusCode.UnprocessableEntity, response.StatusCode);
        Assert.Equal("validation_error", (await Json(response)).GetProperty("error").GetProperty("code").GetString());
    }

    [Theory]
    [InlineData("{not json")]
    [InlineData("[\"https://example.com\"]")]
    public async Task Create_rejects_non_json_and_non_object(string raw)
    {
        var response = await _client.PostAsync("/api/v1/links", new StringContent(raw, Encoding.UTF8, "application/json"));
        Assert.Equal(HttpStatusCode.BadRequest, response.StatusCode);
    }

    [Fact]
    public async Task Create_rejects_oversized_body()
    {
        var response = await _client.PostAsync(
            "/api/v1/links", new StringContent(new string('x', 20000), Encoding.UTF8, "application/json"));
        Assert.Equal(HttpStatusCode.RequestEntityTooLarge, response.StatusCode);
    }

    [Fact]
    public async Task Redirect_and_click_recorded()
    {
        var code = (await Json(await Create(new { url = "https://example.com/dest" }))).GetProperty("code").GetString();
        var request = new HttpRequestMessage(HttpMethod.Get, $"/{code}");
        request.Headers.Referrer = new Uri("https://news.example/item?id=1");
        var response = await _client.SendAsync(request);

        Assert.Equal(HttpStatusCode.Redirect, response.StatusCode);
        Assert.Equal("https://example.com/dest", response.Headers.Location!.ToString());
        Assert.True(response.Headers.CacheControl!.NoStore);

        var stats = await Json(await _client.GetAsync($"/api/v1/links/{code}/stats"));
        Assert.Equal(1, stats.GetProperty("totalClicks").GetInt32());
        var top = stats.GetProperty("topReferrers")[0];
        Assert.Equal("news.example", top.GetProperty("referrer").GetString());
        Assert.Equal(1, top.GetProperty("clicks").GetInt32());
    }

    [Fact]
    public async Task Unknown_code_404()
    {
        Assert.Equal(HttpStatusCode.NotFound, (await _client.GetAsync("/doesnotexist")).StatusCode);
        Assert.Equal(HttpStatusCode.NotFound, (await _client.GetAsync("/api/v1/links/doesnotexist")).StatusCode);
        Assert.Equal(HttpStatusCode.NotFound, (await _client.GetAsync("/api/v1/links/doesnotexist/stats")).StatusCode);
    }

    [Fact]
    public async Task Delete()
    {
        var code = (await Json(await Create(new { url = "https://example.com" }))).GetProperty("code").GetString();
        Assert.Equal(HttpStatusCode.NoContent, (await _client.DeleteAsync($"/api/v1/links/{code}")).StatusCode);
        Assert.Equal(HttpStatusCode.NotFound, (await _client.GetAsync($"/{code}")).StatusCode);
        Assert.Equal(HttpStatusCode.NotFound, (await _client.DeleteAsync($"/api/v1/links/{code}")).StatusCode);
    }

    [Fact]
    public async Task Request_id_propagated()
    {
        var request = new HttpRequestMessage(HttpMethod.Get, "/healthz");
        request.Headers.Add("x-request-id", "abc-123");
        var response = await _client.SendAsync(request);
        Assert.Equal("abc-123", response.Headers.GetValues("x-request-id").Single());

        var generated = await _client.GetAsync("/healthz");
        Assert.False(string.IsNullOrEmpty(generated.Headers.GetValues("x-request-id").Single()));
    }
}
