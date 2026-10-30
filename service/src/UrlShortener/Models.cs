using System.Text.RegularExpressions;

namespace UrlShortener;

/// <summary>Request body for POST /api/v1/links.</summary>
public sealed record CreateLinkRequest(string? Url, string? Alias);

public sealed record Link(string Code, string Url, DateTimeOffset CreatedAt);

public sealed record Click(string Code, DateTimeOffset ClickedAt, string? ReferrerHost, string? IpHash, string? UserAgent);

public sealed record ReferrerCount(string Referrer, int Clicks);

public sealed record LinkStats(
    string Code,
    string Url,
    DateTimeOffset CreatedAt,
    int TotalClicks,
    DateTimeOffset? LastClickedAt,
    IReadOnlyDictionary<string, int> ClicksByDay,
    IReadOnlyList<ReferrerCount> TopReferrers);

public sealed record FieldError(string Field, string Message);

/// <summary>Input validation for new links: destination URL rules and custom alias rules.</summary>
public static class LinkValidation
{
    public const int MaxUrlLength = 2048;

    public static readonly Regex AliasPattern = new("^[A-Za-z0-9_-]{3,32}$", RegexOptions.Compiled);

    // Aliases that would shadow API or operational routes.
    public static readonly HashSet<string> ReservedAliases = new(StringComparer.OrdinalIgnoreCase)
    {
        "api", "healthz", "readyz", "docs", "static", "admin", "openapi",
    };

    public static IReadOnlyList<FieldError> Validate(CreateLinkRequest request, out string url)
    {
        var errors = new List<FieldError>();
        url = (request.Url ?? string.Empty).Trim();

        if (url.Length == 0)
        {
            errors.Add(new FieldError("url", "url must not be empty"));
        }
        else if (url.Length > MaxUrlLength)
        {
            errors.Add(new FieldError("url", $"url must be at most {MaxUrlLength} characters"));
        }
        else if (url.Any(char.IsWhiteSpace))
        {
            errors.Add(new FieldError("url", "url must not contain whitespace"));
        }
        else if (!Uri.TryCreate(url, UriKind.Absolute, out var uri)
                 || (uri.Scheme != Uri.UriSchemeHttp && uri.Scheme != Uri.UriSchemeHttps))
        {
            errors.Add(new FieldError("url", "url must be an absolute http or https URL"));
        }
        else if (uri is null || string.IsNullOrEmpty(uri.Host))
        {
            errors.Add(new FieldError("url", "url must include a host"));
        }

        if (request.Alias is not null)
        {
            if (!AliasPattern.IsMatch(request.Alias))
            {
                errors.Add(new FieldError("alias", "alias must be 3-32 characters of letters, digits, '-' or '_'"));
            }
            else if (ReservedAliases.Contains(request.Alias))
            {
                errors.Add(new FieldError("alias", $"alias '{request.Alias}' is reserved"));
            }
        }

        return errors;
    }
}
