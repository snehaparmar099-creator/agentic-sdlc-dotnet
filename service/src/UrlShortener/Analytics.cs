using System.Security.Cryptography;
using System.Text;

namespace UrlShortener;

/// <summary>
/// Click analytics helpers. Privacy: client IPs are stored only as salted SHA-256 hashes
/// (enough to estimate unique visitors, not enough to recover the address), and referrers
/// are reduced to their host so query strings with tokens or PII are never persisted.
/// </summary>
public static class ClickAnalytics
{
    public const int MaxUserAgentLength = 256;

    public static string? HashIp(string? ip, string salt)
    {
        if (string.IsNullOrEmpty(ip))
        {
            return null;
        }

        var digest = SHA256.HashData(Encoding.UTF8.GetBytes($"{salt}:{ip}"));
        return Convert.ToHexString(digest)[..32].ToLowerInvariant();
    }

    public static string? ReferrerHost(string? referrer)
    {
        if (string.IsNullOrWhiteSpace(referrer))
        {
            return null;
        }

        return Uri.TryCreate(referrer, UriKind.Absolute, out var uri) && !string.IsNullOrEmpty(uri.Host)
            ? uri.Host.ToLowerInvariant()
            : null;
    }

    public static Click BuildClick(
        string code, DateTimeOffset at, string? ip, string? referrer, string? userAgent, string salt)
    {
        var agent = string.IsNullOrEmpty(userAgent)
            ? null
            : userAgent.Length > MaxUserAgentLength ? userAgent[..MaxUserAgentLength] : userAgent;

        return new Click(
            code,
            at,
            ReferrerHost(referrer),
            HashIp(ip, salt),
            agent);
    }
}
