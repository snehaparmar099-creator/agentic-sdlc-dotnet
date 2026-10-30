using Microsoft.AspNetCore.Mvc.Testing;
using Microsoft.AspNetCore.TestHost;
using Microsoft.Extensions.DependencyInjection;

namespace UrlShortener.Tests;

/// <summary>Hosts the real application in memory with test settings (and optionally a fake clock).</summary>
public static class TestApp
{
    public static WebApplicationFactory<Program> Create(ShortenerSettings settings, IClock? clock = null) =>
        new WebApplicationFactory<Program>().WithWebHostBuilder(builder =>
            builder.ConfigureTestServices(services =>
            {
                services.AddSingleton(settings);
                services.AddSingleton<ILinkRepository>(_ => new SqliteLinkRepository(settings.DbPath));
                if (clock is not null)
                {
                    services.AddSingleton(clock);
                }
            }));

    public static HttpClient Client(WebApplicationFactory<Program> factory) =>
        factory.CreateClient(new WebApplicationFactoryClientOptions { AllowAutoRedirect = false });
}
