namespace UrlShortener;

/// <summary>Time source, injectable so tests can control "now".</summary>
public interface IClock
{
    DateTimeOffset UtcNow();
}

public sealed class UtcClock : IClock
{
    public DateTimeOffset UtcNow() => DateTimeOffset.UtcNow;
}
