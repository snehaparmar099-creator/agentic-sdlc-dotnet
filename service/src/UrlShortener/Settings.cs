namespace UrlShortener;

/// <summary>Runtime configuration, loaded from environment variables with safe defaults.</summary>
public sealed record ShortenerSettings
{
    public string BaseUrl { get; init; } = "http://localhost:5000";
    public string DbPath { get; init; } = "urlshortener.db";
    public int CodeLength { get; init; } = 7;
    public int MaxCodeAttempts { get; init; } = 5;

    // Salt for hashing client IPs in analytics. Raw IPs are never stored.
    public string IpHashSalt { get; init; } = "dev-only-salt-change-me";

    public static ShortenerSettings FromEnvironment()
    {
        var defaults = new ShortenerSettings();
        return new ShortenerSettings
        {
            BaseUrl = (Environment.GetEnvironmentVariable("SHORTENER_BASE_URL") ?? defaults.BaseUrl).TrimEnd('/'),
            DbPath = Environment.GetEnvironmentVariable("SHORTENER_DB_PATH") ?? defaults.DbPath,
            CodeLength = IntFromEnv("SHORTENER_CODE_LENGTH", defaults.CodeLength),
            MaxCodeAttempts = IntFromEnv("SHORTENER_MAX_CODE_ATTEMPTS", defaults.MaxCodeAttempts),
            IpHashSalt = Environment.GetEnvironmentVariable("SHORTENER_IP_HASH_SALT") ?? defaults.IpHashSalt,
        };
    }

    private static int IntFromEnv(string name, int fallback) =>
        int.TryParse(Environment.GetEnvironmentVariable(name), out var value) ? value : fallback;
}
