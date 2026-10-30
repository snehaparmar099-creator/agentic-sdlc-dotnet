namespace UrlShortener;

/// <summary>Raised when no free code could be generated within the retry budget.</summary>
public sealed class CodeSpaceExhaustedException : Exception
{
    public CodeSpaceExhaustedException()
        : base("could not allocate a unique short code")
    {
    }
}

/// <summary>Domain service: the business rules between the HTTP layer and storage.</summary>
public sealed class LinkService
{
    private readonly ILinkRepository _repository;
    private readonly ShortenerSettings _settings;
    private readonly IClock _clock;
    private readonly Func<int, string> _generate;

    public LinkService(
        ILinkRepository repository,
        ShortenerSettings settings,
        IClock? clock = null,
        Func<int, string>? codeGenerator = null)
    {
        _repository = repository;
        _settings = settings;
        _clock = clock ?? new UtcClock();
        _generate = codeGenerator ?? (length => CodeGenerator.Generate(length));
    }

    public Link Shorten(string url, string? alias)
    {
        var now = _clock.UtcNow();
        if (alias is not null)
        {
            // Custom aliases never overwrite an existing link.
            return _repository.Create(new Link(alias, url, now));
        }

        for (var attempt = 0; attempt < _settings.MaxCodeAttempts; attempt++)
        {
            var code = _generate(_settings.CodeLength);
            try
            {
                return _repository.Create(new Link(code, url, now));
            }
            catch (AliasConflictException)
            {
                // Collision on a generated code: try another one.
            }
        }

        throw new CodeSpaceExhaustedException();
    }

    public Link? Resolve(string code) => _repository.Get(code);

    public bool Delete(string code) => _repository.Delete(code);

    public void RecordClick(string code, string? ip, string? referrer, string? userAgent) =>
        _repository.RecordClick(
            ClickAnalytics.BuildClick(code, _clock.UtcNow(), ip, referrer, userAgent, _settings.IpHashSalt));

    public LinkStats? Stats(string code) => _repository.Stats(code);
}
