namespace UrlShortener.Tests;

public class LinkServiceTests
{
    private static (LinkService Service, SqliteLinkRepository Repo, FakeClock Clock) Create(
        Func<int, string>? codeGenerator = null)
    {
        var repo = new SqliteLinkRepository(":memory:");
        var clock = new FakeClock();
        return (new LinkService(repo, TestSettings.InMemory(), clock, codeGenerator), repo, clock);
    }

    [Fact]
    public void Shorten_generates_code()
    {
        var (service, repo, _) = Create();
        using (repo)
        {
            var link = service.Shorten("https://example.com", null);
            Assert.Equal(7, link.Code.Length);
            Assert.Equal("https://example.com", service.Resolve(link.Code)!.Url);
        }
    }

    [Fact]
    public void Shorten_with_alias()
    {
        var (service, repo, _) = Create();
        using (repo)
        {
            Assert.Equal("docs-v2", service.Shorten("https://example.com", "docs-v2").Code);
        }
    }

    [Fact]
    public void Alias_conflict_does_not_hijack_existing_link()
    {
        var (service, repo, _) = Create();
        using (repo)
        {
            service.Shorten("https://owner.example", "promo");
            Assert.Throws<AliasConflictException>(() => service.Shorten("https://attacker.example", "promo"));
            Assert.Equal("https://owner.example", service.Resolve("promo")!.Url);
        }
    }

    [Fact]
    public void Generated_code_collision_is_retried()
    {
        var codes = new Queue<string>(new[] { "dupe000", "dupe000", "fresh00" });
        var (service, repo, _) = Create(_ => codes.Dequeue());
        using (repo)
        {
            Assert.Equal("dupe000", service.Shorten("https://a.example", null).Code);
            Assert.Equal("fresh00", service.Shorten("https://b.example", null).Code);
        }
    }

    [Fact]
    public void Collision_retries_are_bounded()
    {
        var (service, repo, _) = Create(_ => "always1");
        using (repo)
        {
            service.Shorten("https://a.example", null);
            Assert.Throws<CodeSpaceExhaustedException>(() => service.Shorten("https://b.example", null));
        }
    }

    [Fact]
    public void Record_click_and_stats()
    {
        var (service, repo, clock) = Create();
        using (repo)
        {
            var link = service.Shorten("https://example.com", null);
            service.RecordClick(link.Code, "1.1.1.1", "https://t.co/x", "ua");
            clock.Advance(TimeSpan.FromDays(1));
            service.RecordClick(link.Code, "1.1.1.1", null, "ua");

            var stats = service.Stats(link.Code)!;
            Assert.Equal(2, stats.TotalClicks);
            Assert.Equal(1, stats.ClicksByDay["2026-01-01"]);
            Assert.Equal(1, stats.ClicksByDay["2026-01-02"]);
            Assert.Single(stats.TopReferrers);
            Assert.Equal(new ReferrerCount("t.co", 1), stats.TopReferrers[0]);
        }
    }
}
