using UrlShortener;

var builder = WebApplication.CreateBuilder(args);

builder.Services.AddSingleton(ShortenerSettings.FromEnvironment());
builder.Services.AddSingleton<IClock, UtcClock>();
builder.Services.AddSingleton<ILinkRepository>(sp =>
    new SqliteLinkRepository(sp.GetRequiredService<ShortenerSettings>().DbPath));
builder.Services.AddSingleton(sp => new LinkService(
    sp.GetRequiredService<ILinkRepository>(),
    sp.GetRequiredService<ShortenerSettings>(),
    sp.GetRequiredService<IClock>()));

var app = builder.Build();

// Propagate or assign a request id on every response.
app.Use(async (context, next) =>
{
    var requestId = context.Request.Headers["x-request-id"].FirstOrDefault();
    if (string.IsNullOrEmpty(requestId))
    {
        requestId = Guid.NewGuid().ToString("N");
    }

    context.Response.Headers["x-request-id"] = requestId;
    await next(context);
});

Endpoints.Map(app);

app.Run();

// Exposed so integration tests can host the app with WebApplicationFactory<Program>.
public partial class Program
{
}
