-- The rules that a restored database must follow, where an exact fact of manifest.sql cannot
-- work (docs/learn/50). restore.sh runs it after the restore. Each line is "ok" or "not ok", a
-- tab, and the fact. No probe raises an error: each one catches what it expects. The checkout runs
-- its own copy against older backups too, so a rule must hold for each revision that it knows.
\pset format unaligned
\pset fieldsep '\t'
\pset tuples_only on
\pset footer off

-- A sequence moves outside of each snapshot, so its value is not a fact of the manifest. But each
-- sequence of an identity column must cover the ids that it gave: a lower value would give an id
-- that a row already has. One line for the rule, and one for each sequence that breaks it.
CREATE FUNCTION pg_temp.identity_sequences() RETURNS TABLE (sequence text, covered boolean)
LANGUAGE plpgsql AS $$
DECLARE
    one record;
BEGIN
    FOR one IN
        SELECT s.oid::regclass AS seq, d.refobjid::regclass AS tab, a.attname AS col
        FROM pg_class s
        JOIN pg_depend d
            ON d.classid = 'pg_class'::regclass AND d.objid = s.oid
            AND d.refclassid = 'pg_class'::regclass AND d.deptype IN ('a', 'i')
        JOIN pg_attribute a ON a.attrelid = d.refobjid AND a.attnum = d.refobjsubid
        WHERE s.relkind = 'S' AND s.relnamespace = 'public'::regnamespace
        ORDER BY 1
    LOOP
        sequence := one.seq::text;
        EXECUTE format(
            'SELECT coalesce((SELECT max(%I) FROM %s), 0)
                <= (SELECT CASE WHEN is_called THEN last_value ELSE last_value - 1 END FROM %s)',
            one.col, one.tab, one.seq
        ) INTO covered;
        RETURN NEXT;
    END LOOP;
END
$$;

WITH sequences AS (SELECT * FROM pg_temp.identity_sequences())
SELECT 'not ok', format('the sequence %s is below an id that it gave', sequence)
FROM sequences WHERE NOT covered
UNION ALL
SELECT 'ok', format('each of the %s sequences of an identity column covers the ids that it gave', count(*))
FROM sequences HAVING bool_and(covered);

-- capture_api_seq belongs to no column: it gives the message ids of the captures of the API.
SELECT CASE WHEN coalesce((SELECT max(message_id) FROM captures WHERE chat_id = 0), 0)
    <= (SELECT CASE WHEN is_called THEN last_value ELSE last_value - 1 END FROM capture_api_seq)
    THEN 'ok' ELSE 'not ok' END,
    'the sequence capture_api_seq covers each message id of the captures of the API';

-- u_norm() makes the text of the search: an index on it needs an IMMUTABLE function, and the
-- function must still map Arabic yeh to Persian yeh, and Arabic-Indic digits to ASCII digits.
SELECT CASE WHEN count(*) = 1 AND bool_and(p.provolatile = 'i') AND u_norm('ي٣') = 'ی3'
    THEN 'ok' ELSE 'not ok' END,
    'u_norm() is IMMUTABLE, and it maps ي٣ to ی3'
FROM pg_proc p
WHERE p.proname = 'u_norm' AND p.pronamespace = 'public'::regnamespace;

-- The events are append-only. Each probe runs in a block that is rolled back: a change that
-- works is undone, and gives false.
CREATE FUNCTION pg_temp.refuses(statement text) RETURNS boolean LANGUAGE plpgsql AS $$
BEGIN
    BEGIN
        EXECUTE statement;
        RAISE EXCEPTION 'the statement worked';
    EXCEPTION
        WHEN restrict_violation THEN RETURN true;
        WHEN raise_exception THEN RETURN false;
    END;
END
$$;

-- The probe of UPDATE needs a row; the triggers must be on also when the table is empty.
SELECT CASE WHEN count(*) = 2 THEN 'ok' ELSE 'not ok' END,
    'the triggers events_append_only and events_no_truncate are on'
FROM pg_trigger
WHERE tgrelid = 'events'::regclass AND tgname IN ('events_append_only', 'events_no_truncate')
    AND tgenabled IN ('O', 'A');
SELECT CASE WHEN pg_temp.refuses('TRUNCATE events') THEN 'ok' ELSE 'not ok' END,
    'the table events refuses TRUNCATE';
SELECT CASE WHEN NOT EXISTS (SELECT 1 FROM events)
    OR pg_temp.refuses('UPDATE events SET type = type WHERE id = (SELECT min(id) FROM events)')
    THEN 'ok' ELSE 'not ok' END,
    'the table events refuses UPDATE';

-- The publisher reads the public view, and nothing of the private tables.
SELECT CASE WHEN has_schema_privilege('unshelve_publisher', 'pub', 'USAGE')
    AND has_table_privilege('unshelve_publisher', 'pub.items', 'SELECT')
    AND NOT has_table_privilege('unshelve_publisher', 'public.items', 'SELECT')
    THEN 'ok' ELSE 'not ok' END,
    'unshelve_publisher reads pub.items, and not public.items';

-- Without the rights above, a read as the publisher would only stop this file with an error.
SELECT has_schema_privilege('unshelve_publisher', 'pub', 'USAGE')
    AND has_table_privilege('unshelve_publisher', 'pub.items', 'SELECT') AS publisher_reads \gset
\if :publisher_reads
BEGIN;
SET LOCAL ROLE unshelve_publisher;
SELECT count(*) AS published FROM pub.items \gset
ROLLBACK;
SELECT CASE WHEN :published = count(*) THEN 'ok' ELSE 'not ok' END,
    format('unshelve_publisher sees the %s public items', :published)
FROM pub.items;
\endif
