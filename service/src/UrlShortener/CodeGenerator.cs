using System.Security.Cryptography;

namespace UrlShortener;

/// <summary>
/// Short-code generation. Codes are random base62 strings from a CSPRNG, so other users'
/// links cannot be enumerated. At 7 characters there are ~3.5e12 codes; collisions are
/// handled by the caller retrying a bounded number of times.
/// </summary>
public static class CodeGenerator
{
    public const string Alphabet = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz";

    public static string Generate(int length = 7)
    {
        if (length < 4)
        {
            throw new ArgumentOutOfRangeException(nameof(length), "code length must be at least 4");
        }

        return RandomNumberGenerator.GetString(Alphabet, length);
    }
}
