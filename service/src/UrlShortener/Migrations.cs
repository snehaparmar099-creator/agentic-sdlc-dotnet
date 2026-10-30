using Microsoft.Data.Sqlite;

namespace UrlShortener;

public sealed record Migration(int Version, string Sql);

/// <summary>
/// Forward-only, versioned schema migrations. Each migration runs once inside a transaction
/// and is recorded in schema_version. New schema changes are appended as a new version;
/// existing entries never change (the orchestrator's change-control policy enforces this).
/// </summary>
public static class SchemaMigrations
{
    public static readonly IReadOnlyList<Migration> All = new List<Migration>
    {
        new Migration(1, @"
CREATE TABLE links (
    code        TEXT PRIMARY KEY,
    url         TEXT NOT NULL,
    created_at  TEXT NOT NULL
);
CREATE TABLE clicks (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    code          TEXT NOT NULL REFERENCES links(code) ON DELETE CASCADE,
    clicked_at    TEXT NOT NULL,
    referrer_host TEXT,
    ip_hash       TEXT,
    user_agent    TEXT
);
CREATE INDEX idx_clicks_code_time ON clicks(code, clicked_at);"),
    };

    public static int CurrentVersion(SqliteConnection connection)
    {
        using var create = connection.CreateCommand();
        create.CommandText = "CREATE TABLE IF NOT EXISTS schema_version (version INTEGER PRIMARY KEY)";
        create.ExecuteNonQuery();

        using var query = connection.CreateCommand();
        query.CommandText = "SELECT MAX(version) FROM schema_version";
        var result = query.ExecuteScalar();
        return result is null || result is DBNull ? 0 : Convert.ToInt32(result);
    }

    /// <summary>Apply pending migrations; returns the resulting schema version.</summary>
    public static int Apply(SqliteConnection connection)
    {
        var version = CurrentVersion(connection);
        foreach (var migration in All)
        {
            if (migration.Version <= version)
            {
                continue;
            }

            using var transaction = connection.BeginTransaction();
            using (var command = connection.CreateCommand())
            {
                command.Transaction = transaction;
                command.CommandText = migration.Sql;
                command.ExecuteNonQuery();
            }

            using (var record = connection.CreateCommand())
            {
                record.Transaction = transaction;
                record.CommandText = "INSERT INTO schema_version (version) VALUES ($version)";
                record.Parameters.AddWithValue("$version", migration.Version);
                record.ExecuteNonQuery();
            }

            transaction.Commit();
            version = migration.Version;
        }

        return version;
    }
}
