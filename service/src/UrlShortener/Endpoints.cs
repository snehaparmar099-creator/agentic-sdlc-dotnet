using System.Text.Json;

namespace UrlShortener;

/// <summary>
/// HTTP API (ASP.NET Core minimal APIs).
///   POST   /api/v1/links               create a short link
///   GET    /api/v1/links/{code}        link metadata
///   DELETE /api/v1/links/{code}        delete a link
///   GET    /api/v1/links/{code}/stats  click analytics
///   GET    /healthz                    liveness
///   GET    /{code}                     redirect (302)
/// </summary>
public static class Endpoints
{
    public const int MaxBodyBytes = 16 * 1024;

    private static readonly JsonSerializerOptions JsonOptions = new(JsonSerializerDefaults.Web);

    public static void Map(WebApplication app)
    {
        app.MapGet("/healthz", Health);
        app.MapPost("/api/v1/links", CreateLink);
        app.MapGet("/api/v1/links/{code}", GetLink);
        app.MapDelete("/api/v1/links/{code}", DeleteLink);
        app.MapGet("/api/v1/links/{code}/stats", GetStats);
        app.MapGet("/{code}", RedirectToDestination);
    }

    public static IResult Error(int status, string code, string message, IReadOnlyList<FieldError>? details = null)
    {
        var error = new Dictionary<string, object?> { ["code"] = code, ["message"] = message };
        if (details is { Count: > 0 })
        {
            error["details"] = details.Select(d => new { field = d.Field, message = d.Message }).ToList();
        }

        return Results.Json(new { error }, statusCode: status);
    }

    public static object LinkJson(Link link, ShortenerSettings settings) => new
    {
        code = link.Code,
        shortUrl = $"{settings.BaseUrl}/{link.Code}",
        url = link.Url,
        createdAt = link.CreatedAt.ToString("o"),
    };

    public static object StatsJson(LinkStats stats) => new
    {
        code = stats.Code,
        url = stats.Url,
        createdAt = stats.CreatedAt.ToString("o"),
        totalClicks = stats.TotalClicks,
        lastClickedAt = stats.LastClickedAt?.ToString("o"),
        clicksByDay = stats.ClicksByDay,
        topReferrers = stats.TopReferrers.Select(r => new { referrer = r.Referrer, clicks = r.Clicks }).ToList(),
    };

    public static IResult Health() => Results.Json(new { status = "ok", version = "1.0.0" });

    public static async Task<IResult> CreateLink(HttpRequest request, LinkService service, ShortenerSettings settings)
    {
        if (request.ContentLength > MaxBodyBytes)
        {
            return Error(413, "payload_too_large", "request body too large");
        }

        string raw;
        using (var reader = new StreamReader(request.Body))
        {
            raw = await reader.ReadToEndAsync();
        }

        if (raw.Length > MaxBodyBytes)
        {
            return Error(413, "payload_too_large", "request body too large");
        }

        CreateLinkRequest? body;
        try
        {
            using var document = JsonDocument.Parse(raw.Length == 0 ? "null" : raw);
            if (document.RootElement.ValueKind != JsonValueKind.Object)
            {
                return Error(400, "invalid_json", "request body must be a JSON object");
            }

            body = document.RootElement.Deserialize<CreateLinkRequest>(JsonOptions);
        }
        catch (JsonException)
        {
            return Error(400, "invalid_json", "request body must be valid JSON");
        }

        if (body is null)
        {
            return Error(400, "invalid_json", "request body must be a JSON object");
        }

        var errors = LinkValidation.Validate(body, out var url);
        if (errors.Count > 0)
        {
            return Error(422, "validation_error", "invalid request", errors);
        }

        Link link;
        try
        {
            link = service.Shorten(url, body.Alias);
        }
        catch (AliasConflictException)
        {
            return Error(409, "alias_conflict", $"alias '{body.Alias}' is already in use");
        }
        catch (CodeSpaceExhaustedException)
        {
            return Error(503, "code_allocation_failed", "try again later");
        }

        return Results.Json(LinkJson(link, settings), statusCode: StatusCodes.Status201Created);
    }

    public static IResult GetLink(string code, LinkService service, ShortenerSettings settings)
    {
        var link = service.Resolve(code);
        return link is null ? Error(404, "not_found", "link not found") : Results.Json(LinkJson(link, settings));
    }

    public static IResult DeleteLink(string code, LinkService service) =>
        service.Delete(code) ? Results.NoContent() : Error(404, "not_found", "link not found");

    public static IResult GetStats(string code, LinkService service)
    {
        var stats = service.Stats(code);
        return stats is null ? Error(404, "not_found", "link not found") : Results.Json(StatsJson(stats));
    }

    public static IResult RedirectToDestination(string code, HttpContext context, LinkService service)
    {
        var link = service.Resolve(code);
        if (link is null)
        {
            return Error(404, "not_found", "link not found");
        }

        service.RecordClick(
            code,
            context.Connection.RemoteIpAddress?.ToString(),
            context.Request.Headers.Referer.ToString(),
            context.Request.Headers.UserAgent.ToString());
        context.Response.Headers.CacheControl = "no-store";
        return Results.Redirect(link.Url);
    }
}
