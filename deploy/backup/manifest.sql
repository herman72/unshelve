-- The facts of the database that a backup and its restore must share (docs/learn/50).
-- backup.sh writes them inside the snapshot of the dump; a restore writes them again and
-- compares the two files line by line. Each line is: kind, name, value(s), separated by tabs.
-- Sequences are not here: their values are not part of a snapshot (checks.sql compares them).
\pset format unaligned
\pset fieldsep '\t'
\pset tuples_only on
\pset footer off
-- The text of a row depends on these settings, so both sides use the same ones.
SET TIME ZONE 'UTC';
SET DateStyle = 'ISO';
SET IntervalStyle = 'postgres';
SET extra_float_digits = 1;
SET bytea_output = 'hex';

SELECT 'revision', version_num FROM alembic_version;

-- The versions of the extensions can be newer after a restore: checks.sql compares them.
SELECT 'extension', extname FROM pg_extension WHERE extname <> 'plpgsql' ORDER BY 2;

-- The roles that the privileges name: pg_dump of one database does not create them.
SELECT DISTINCT 'role', r.rolname, r.rolcanlogin
FROM (
    SELECT (aclexplode(nspacl)).grantee FROM pg_namespace
    WHERE nspname IN ('public', 'pub') AND nspacl IS NOT NULL
    UNION
    SELECT (aclexplode(c.relacl)).grantee FROM pg_class c
    JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE n.nspname IN ('public', 'pub') AND c.relacl IS NOT NULL
) granted
JOIN pg_roles r ON r.oid = granted.grantee
WHERE r.rolname <> current_user
ORDER BY 2;

-- Each table: its rows and one fingerprint of all of them, in any order of the rows.
SELECT format(
    $sql$SELECT 'table', %L, count(*), coalesce(md5(string_agg(h, '' ORDER BY h)), '')
    FROM (SELECT md5(t::text) AS h FROM %I.%I t) rows$sql$,
    schemaname || '.' || tablename, schemaname, tablename
)
FROM pg_tables
WHERE schemaname IN ('public', 'pub')
ORDER BY schemaname, tablename
\gexec

SELECT 'index', indexname, indexdef FROM pg_indexes
WHERE schemaname IN ('public', 'pub') ORDER BY 2;

SELECT 'constraint', conrelid::regclass::text, conname, pg_get_constraintdef(oid)
FROM pg_constraint
WHERE connamespace IN ('public'::regnamespace, 'pub'::regnamespace)
ORDER BY 2, 3;

SELECT 'trigger', tgrelid::regclass::text, tgname, tgenabled
FROM pg_trigger WHERE NOT tgisinternal ORDER BY 2, 3;

-- The functions of this schema, not those of an extension, such as u_norm().
SELECT 'function', p.proname, md5(pg_get_functiondef(p.oid))
FROM pg_proc p
WHERE p.pronamespace = 'public'::regnamespace
  AND NOT EXISTS (SELECT 1 FROM pg_depend d WHERE d.objid = p.oid AND d.deptype = 'e')
ORDER BY 2, 3;

SELECT 'view', c.oid::regclass::text, md5(pg_get_viewdef(c.oid)), coalesce(c.relacl::text, '')
FROM pg_class c
WHERE c.relkind = 'v' AND c.relnamespace IN ('public'::regnamespace, 'pub'::regnamespace)
ORDER BY 2;

SELECT 'schema', nspname, coalesce(nspacl::text, '')
FROM pg_namespace WHERE nspname = 'pub';

SELECT 'public-items', count(*) FROM pub.items;
