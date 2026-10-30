namespace UrlShortener.Tests;

/// <summary>Controllable clock for deterministic tests.</summary>
public sealed class FakeClock : IClock
{
    public DateTimeOffset Now { get; set; } = new(2026, 1, 1, 12, 0, 0, TimeSpan.Zero);

    public DateTimeOffset UtcNow() => Now;

    public void Advance(TimeSpan by) => Now += by;
}

public static class TestSettings
{
    public static ShortenerSettings InMemory() => new()
    {
        BaseUrl = "https://sho.rt",
        DbPath = ":memory:",
        IpHashSalt = "test-salt",
    };
}
