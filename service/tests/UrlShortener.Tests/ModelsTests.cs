namespace UrlShortener.Tests;

public class ModelsTests
{
    private static IReadOnlyList<FieldError> Validate(string? url, string? alias = null) =>
        LinkValidation.Validate(new CreateLinkRequest(url, alias), out _);

    [Theory]
    [InlineData("https://example.com")]
    [InlineData("http://example.com/a?b=c#d")]
    [InlineData("https://sub.example.co.uk:8443/path")]
    public void Accepts_http_and_https(string url)
    {
        var errors = LinkValidation.Validate(new CreateLinkRequest(url, null), out var normalized);
        Assert.Empty(errors);
        Assert.Equal(url, normalized);
    }

    [Theory]
    [InlineData("")]
    [InlineData("ftp://example.com")]
    [InlineData("javascript:alert(1)")]
    [InlineData("data:text/html,hi")]
    [InlineData("https://")]
    [InlineData("example.com")]
    [InlineData("https://exa mple.com")]
    public void Rejects_unsafe_or_malformed_urls(string url)
    {
        Assert.NotEmpty(Validate(url));
    }

    [Fact]
    public void Rejects_overlong_url()
    {
        Assert.NotEmpty(Validate("https://example.com/" + new string('a', 2100)));
    }

    [Fact]
    public void Rejects_missing_url()
    {
        Assert.Contains(Validate(null), e => e.Field == "url");
    }

    [Fact]
    public void Strips_surrounding_whitespace()
    {
        var errors = LinkValidation.Validate(new CreateLinkRequest("  https://example.com  ", null), out var normalized);
        Assert.Empty(errors);
        Assert.Equal("https://example.com", normalized);
    }

    [Theory]
    [InlineData("abc")]
    [InlineData("my-link_01")]
    [InlineData("AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA")]
    public void Accepts_valid_alias(string alias)
    {
        Assert.Empty(Validate("https://example.com", alias));
    }

    [Theory]
    [InlineData("ab")]
    [InlineData("AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA")]
    [InlineData("has space")]
    [InlineData("slash/es")]
    [InlineData("api")]
    [InlineData("HEALTHZ")]
    public void Rejects_invalid_or_reserved_alias(string alias)
    {
        Assert.Contains(Validate("https://example.com", alias), e => e.Field == "alias");
    }
}
