-- One backup of the database from one snapshot (docs/learn/50). backup.sh runs it with psql
-- in the folder of the new backup. The manifest, the list of blob keys, the roles and the
-- dump all see the same data.
\set ON_ERROR_STOP on
-- The names of the tables, before the transaction: a query inside it would take its snapshot.
SELECT string_agg(format('%I.%I', schemaname, tablename), ', ' ORDER BY schemaname, tablename)
    AS tables
FROM pg_tables
WHERE schemaname IN ('public', 'pub') \gset

-- The locks come first, and the snapshot after them. A lock that only a change of a table (DDL)
-- waits for: the writes of the app do not wait. If a migration rewrites a table now, the lock
-- waits for its commit. A snapshot from before that commit would see the rewritten table as
-- empty, because its new rows are newer than the snapshot. LOCK takes no snapshot.
BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY;
SET LOCAL lock_timeout = '60s';
LOCK TABLE :tables IN ACCESS SHARE MODE;
SELECT pg_export_snapshot() AS snapshot \gset

-- The dump comes first, while no change of a table waits in the queue: pg_dump takes its own
-- locks at its start, and a change that came after the locks above could otherwise stand
-- between them. psql does not put variables into a shell command, so the snapshot goes
-- through the environment. A failed command does not stop psql: backup.sh requires dump.ok.
\setenv UNSHELVE_SNAPSHOT :snapshot
\! pg_dump --format=custom --snapshot="$UNSHELVE_SNAPSHOT" --lock-wait-timeout=60s --file=unshelve.dump && touch dump.ok

-- The facts and the keys come from the same snapshot, after the dump.
\o manifest.tsv
\ir manifest.sql

-- The keys of the files that the rows name: the files that a restore must have.
\o blob-keys.txt
SELECT key FROM (
    SELECT storage_key AS key FROM assets
    UNION
    SELECT m ->> 'sha256' FROM captures, jsonb_array_elements(media) AS m
) keys
WHERE key ~ '^[0-9a-f]{64}$'
ORDER BY key;

\o info.txt
SELECT 'server', current_setting('server_version');
SELECT 'revision', version_num FROM alembic_version;
SELECT 'extension', extname, extversion FROM pg_extension WHERE extname <> 'plpgsql' ORDER BY 2;
SELECT 'jobs', state, count(*) FROM jobs GROUP BY state ORDER BY 2;
SELECT 'items', count(*) FROM items;
SELECT 'assets', count(*) FROM assets;
SELECT 'llm_calls', count(*) FROM llm_calls;
\o

COMMIT;
