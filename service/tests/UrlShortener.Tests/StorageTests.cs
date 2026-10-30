using Microsoft.Data.Sqlite;

namespace UrlShortener.Tests;

public class StorageTests
{
    private static readonly DateTimeOffset T0 = new(2026, 1, 1, 12, 0, 0, TimeSpan.Zero);

    [Fact]
    public void Migrations_are_idempotent()
    {
        using var connection = new SqliteConnection("Data Source=:memory:");
        connection.Open();
        var latest = SchemaMigrations.All[^1].Version;
        Assert.Equal(latest, SchemaMigrations.Apply(connection));
        Assert.Equal(latest, SchemaMigrations.Apply(connection));
        Assert.Equal(latest, SchemaMigrations.CurrentVersion(connection));
    }

    [Fact]
    public void Create_and_get_roundtrip()
    {
        using var repo = new SqliteLinkRepository(":memory:");
        repo.Create(new Link("abc1234", "https://example.com", T0));
        Assert.Equal(new Link("abc1234", "https://example.com", T0), repo.Get("abc1234"));
    }

    [Fact]
    public void Duplicate_code_raises_and_does_not_overwrite()
    {
        using var repo = new SqliteLinkRepository(":memory:");
        repo.Create(new Link("taken", "https://first.example", T0));
        Assert.Throws<AliasConflictException>(() => repo.Create(new Link("taken", "https://second.example", T0)));
        Assert.Equal("https://first.example", repo.Get("taken")!.Url);
    }

    [Fact]
    public void Get_missing_returns_null()
    {
        using var repo = new SqliteLinkRepository(":memory:");
        Assert.Null(repo.Get("nope"));
    }

    [Fact]
    public void Delete_removes_link_and_clicks()
    {
        using var repo = new SqliteLinkRepository(":memory:");
        repo.Create(new Link("gone", "https://example.com", T0));
        repo.RecordClick(new Click("gone", T0, null, null, null));
        Assert.True(repo.Delete("gone"));
        Assert.Null(repo.Get("gone"));
        Assert.False(repo.Delete("gone"));
    }

    [Fact]
    public void Stats_aggregates_clicks()
    {
        using var repo = new SqliteLinkRepository(":memory:");
        repo.Create(new Link("s1", "https://example.com", T0));
        var day2 = T0.AddDays(1);
        repo.RecordClick(new Click("s1", T0, "news.ycombinator.com", "h", "ua"));
        repo.RecordClick(new Click("s1", T0, "news.ycombinator.com", "h", "ua"));
        repo.RecordClick(new Click("s1", day2, "t.co", "h", "ua"));
        repo.RecordClick(new Click("s1", day2, null, "h", "ua"));

        var stats = repo.Stats("s1")!;
        Assert.Equal(4, stats.TotalClicks);
        Assert.Equal(2, stats.ClicksByDay.Count);
        Assert.Equal(2, stats.ClicksByDay["2026-01-01"]);
        Assert.Equal(2, stats.ClicksByDay["2026-01-02"]);
        Assert.Equal(2, stats.TopReferrers.Count);
        Assert.Equal(new ReferrerCount("news.ycombinator.com", 2), stats.TopReferrers[0]);
        Assert.Equal(new ReferrerCount("t.co", 1), stats.TopReferrers[1]);
        Assert.Equal(day2, stats.LastClickedAt);
    }

    [Fact]
    public void Stats_for_link_without_clicks()
    {
        using var repo = new SqliteLinkRepository(":memory:");
        repo.Create(new Link("quiet", "https://example.com", T0));
        var stats = repo.Stats("quiet")!;
        Assert.Equal(0, stats.TotalClicks);
        Assert.Null(stats.LastClickedAt);
        Assert.Empty(stats.ClicksByDay);
    }

    [Fact]
    public void Stats_missing_link_is_null()
    {
        using var repo = new SqliteLinkRepository(":memory:");
        Assert.Null(repo.Stats("missing"));
    }

    [Fact]
    public void File_backed_database_persists()
    {
        var path = Path.Combine(Path.GetTempPath(), $"links-{Guid.NewGuid():N}.db");
        using (var first = new SqliteLinkRepository(path))
        {
            first.Create(new Link("keep", "https://example.com", T0));
        }

        using var second = new SqliteLinkRepository(path);
        Assert.NotNull(second.Get("keep"));
    }
}
