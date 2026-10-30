using System.Globalization;
using Microsoft.Data.Sqlite;

namespace UrlShortener;

/// <summary>
/// Persistence seam the service depends on. SqliteLinkRepository is the bundled
/// implementation; a Postgres implementation can be added behind the same interface
/// without touching the service or HTTP layers.
/// </summary>
public interface ILinkRepository
{
    Link Create(Link link);
    Link? Get(string code);
    bool Delete(string code);
    void RecordClick(Click click);
    LinkStats? Stats(string code, int topReferrers = 5);
}

/// <summary>Raised when a short code is already taken.</summary>
public sealed class AliasConflictException : Exception
{
    public AliasConflictException(string code)
        : base($"code '{code}' is already in use")
    {
        Code = code;
    }

    public string Code { get; }
}

/// <summary>
/// SQLite repository. One connection is shared and guarded by a lock, which also makes
/// in-memory databases (":memory:") work for tests. All SQL is parameterized.
/// </summary>
public sealed class SqliteLinkRepository : ILinkRepository, IDisposable
{
    private const int SqliteConstraintError = 19;
    private readonly object _gate = new();
    private readonly SqliteConnection _connection;

    public SqliteLinkRepository(string dbPath)
    {
        var connectionString = new SqliteConnectionStringBuilder { DataSource = dbPath }.ToString();
        _connection = new SqliteConnection(connectionString);
        _connection.Open();
        Execute("PRAGMA foreign_keys = ON;");
        if (dbPath != ":memory:")
        {
            Execute("PRAGMA journal_mode = WAL;");
        }

        SchemaMigrations.Apply(_connection);
    }

    public Link Create(Link link)
    {
        lock (_gate)
        {
            using var command = Command(
                "INSERT INTO links (code, url, created_at) VALUES ($code, $url, $created)",
                ("$code", link.Code),
                ("$url", link.Url),
                ("$created", Iso(link.CreatedAt)));
            try
            {
                command.ExecuteNonQuery();
            }
            catch (SqliteException ex) when (ex.SqliteErrorCode == SqliteConstraintError)
            {
                throw new AliasConflictException(link.Code);
            }
        }

        return link;
    }

    public Link? Get(string code)
    {
        lock (_gate)
        {
            using var command = Command("SELECT code, url, created_at FROM links WHERE code = $code", ("$code", code));
            using var reader = command.ExecuteReader();
            if (!reader.Read())
            {
                return null;
            }

            return new Link(reader.GetString(0), reader.GetString(1), Parse(reader.GetString(2)));
        }
    }

    public bool Delete(string code)
    {
        lock (_gate)
        {
            using var command = Command("DELETE FROM links WHERE code = $code", ("$code", code));
            return command.ExecuteNonQuery() > 0;
        }
    }

    public void RecordClick(Click click)
    {
        lock (_gate)
        {
            using var command = Command(
                "INSERT INTO clicks (code, clicked_at, referrer_host, ip_hash, user_agent) "
                + "VALUES ($code, $at, $referrer, $ip, $agent)",
                ("$code", click.Code),
                ("$at", Iso(click.ClickedAt)),
                ("$referrer", click.ReferrerHost),
                ("$ip", click.IpHash),
                ("$agent", click.UserAgent));
            command.ExecuteNonQuery();
        }
    }

    public LinkStats? Stats(string code, int topReferrers = 5)
    {
        var link = Get(code);
        if (link is null)
        {
            return null;
        }

        lock (_gate)
        {
            long total;
            string? last;
            using (var command = Command(
                "SELECT COUNT(*), MAX(clicked_at) FROM clicks WHERE code = $code", ("$code", code)))
            using (var reader = command.ExecuteReader())
            {
                reader.Read();
                total = reader.GetInt64(0);
                last = reader.IsDBNull(1) ? null : reader.GetString(1);
            }

            var byDay = new SortedDictionary<string, int>(StringComparer.Ordinal);
            using (var command = Command(
                "SELECT substr(clicked_at, 1, 10) AS day, COUNT(*) FROM clicks "
                + "WHERE code = $code GROUP BY day ORDER BY day",
                ("$code", code)))
            using (var reader = command.ExecuteReader())
            {
                while (reader.Read())
                {
                    byDay[reader.GetString(0)] = (int)reader.GetInt64(1);
                }
            }

            var referrers = new List<ReferrerCount>();
            using (var command = Command(
                "SELECT referrer_host, COUNT(*) AS n FROM clicks "
                + "WHERE code = $code AND referrer_host IS NOT NULL "
                + "GROUP BY referrer_host ORDER BY n DESC, referrer_host LIMIT $limit",
                ("$code", code),
                ("$limit", topReferrers)))
            using (var reader = command.ExecuteReader())
            {
                while (reader.Read())
                {
                    referrers.Add(new ReferrerCount(reader.GetString(0), (int)reader.GetInt64(1)));
                }
            }

            return new LinkStats(
                link.Code,
                link.Url,
                link.CreatedAt,
                (int)total,
                last is null ? null : Parse(last),
                byDay,
                referrers);
        }
    }

    public void Dispose()
    {
        lock (_gate)
        {
            _connection.Dispose();
        }
    }

    private void Execute(string sql)
    {
        using var command = _connection.CreateCommand();
        command.CommandText = sql;
        command.ExecuteNonQuery();
    }

    private SqliteCommand Command(string sql, params (string Name, object? Value)[] parameters)
    {
        var command = _connection.CreateCommand();
        command.CommandText = sql;
        foreach (var (name, value) in parameters)
        {
            command.Parameters.AddWithValue(name, value ?? DBNull.Value);
        }

        return command;
    }

    private static string Iso(DateTimeOffset value) =>
        value.ToUniversalTime().ToString("o", CultureInfo.InvariantCulture);

    private static DateTimeOffset Parse(string value) =>
        DateTimeOffset.Parse(value, CultureInfo.InvariantCulture);
}
