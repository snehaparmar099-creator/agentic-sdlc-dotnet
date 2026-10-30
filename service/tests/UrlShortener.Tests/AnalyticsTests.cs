namespace UrlShortener.Tests;

public class AnalyticsTests
{
    private static readonly DateTimeOffset T0 = new(2026, 1, 1, 0, 0, 0, TimeSpan.Zero);

    [Fact]
    public void Ip_hash_is_salted_and_stable()
    {
        Assert.Equal(ClickAnalytics.HashIp("1.2.3.4", "a"), ClickAnalytics.HashIp("1.2.3.4", "a"));
        Assert.NotEqual(ClickAnalytics.HashIp("1.2.3.4", "a"), ClickAnalytics.HashIp("1.2.3.4", "b"));
        Assert.DoesNotContain("1.2.3.4", ClickAnalytics.HashIp("1.2.3.4", "a")!);
        Assert.Null(ClickAnalytics.HashIp(null, "a"));
    }

    [Fact]
    public void Referrer_reduced_to_host()
    {
        Assert.Equal("news.example.com", ClickAnalytics.ReferrerHost("https://News.Example.com/item?id=1&token=secret"));
        Assert.Null(ClickAnalytics.ReferrerHost(""));
        Assert.Null(ClickAnalytics.ReferrerHost("not a url"));
    }

    [Fact]
    public void Build_click_never_stores_raw_ip_or_full_referrer()
    {
        var click = ClickAnalytics.BuildClick(
            "c", T0, "10.0.0.1", "https://a.com/p?email=x@y.z", new string('x', 1000), "s");
        Assert.NotNull(click.IpHash);
        Assert.NotEqual("10.0.0.1", click.IpHash);
        Assert.Equal("a.com", click.ReferrerHost);
        Assert.Equal(256, click.UserAgent!.Length);
    }
}
