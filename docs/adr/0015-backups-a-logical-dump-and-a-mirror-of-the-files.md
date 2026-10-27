# 0015. Backups: a logical dump from one snapshot, and a mirror of the files

- Status: Accepted
- Date: 2026-10-27

## Context

The instance holds data that cannot come back from anywhere else: the items, the labels of the owner, the files that the owner sent, and the answers of the model, which cost money. Before #40, all of it lived only in two Docker volumes of one computer (`db-data` and `blobs`), and `docker compose down --volumes` deleted both.

Four facts shaped the decision:

- **One owner, one computer, a laptop today.** The hosting is not decided yet (#42). A backup must work on a Mac that sleeps and on a Linux server, with the tools of the stack.
- **The files are content-addressed.** The name of each file in the blob store is the SHA-256 of its bytes, and the app never deletes or changes a file. So a copy of a file never goes out of date.
- **The database has parts that the ORM does not model.** Migration 0001 creates the extensions `vector` and `pg_trgm`, the IMMUTABLE function `u_norm()`, the append-only trigger of `events`, the view `pub.items`, and the cluster-wide role `unshelve_publisher` with its GRANTs. A backup must keep each of them.
- **A backup that is never restored is a hope.** The acceptance of #40 is a restore into an empty instance that passes a data check.

Three designs were compared, and three judges scored them for restore safety, simplicity for one owner, and security and cost: a Postgres-native service (21.5 of 30), an encrypted copy in object storage first (17), and typed `unshelve backup` commands (16.5).

## Decision

- **A service `backup` in the stack** uses the image of the database, so `pg_dump` always has the major version of the server. It gets the password of the database by name from `.env`, and no other secret.
- **One snapshot for each backup.** `snapshot.sql` opens a REPEATABLE READ, READ ONLY transaction, takes ACCESS SHARE locks on each table, which only a change of a table waits for, and then exports its snapshot. The locks come first: a snapshot from before the commit of a table rewrite would see that table as empty. In that snapshot it writes the dump (`pg_dump --format=custom --snapshot`), then a manifest of facts (the rows of each table as a count and a fingerprint, the indexes, the constraints, the triggers, the functions, the view, the roles) and the keys of the files that the rows name. pg_dump starts right after the locks, because a migration that waits could otherwise get in front of its own locks.
- **One mirror of the files** for all the backups. A copy gets its name only after its SHA-256 equals the name, and the mirror is never pruned, because the app never deletes a file.
- **A folder of the computer, not a volume**, holds the backups (`backups/`, or `UNSHELVE_BACKUP_DIR`), so `down --volumes` cannot delete them. A backup is written as `.part-<time>`, synced to the disk, and renamed when it is complete. A failed run removes its part.
- **A backup when the newest one is older than a day.** The service checks every 5 minutes, so a computer that slept makes the backup when it wakes. It keeps the 7 newest backups and the newest one of each of the last 6 calendar weeks (Monday to Sunday, UTC), so the oldest backup is 29 to 35 days old.
- **Failures are visible.** The service is unhealthy when its last run failed, when no backup worked for two days (one hour after a start or a restart), or when a backup has a name in the future; `backup.sh status` says why. While it is unhealthy, `docker compose up --wait` ends with an error.
- **A restore that checks before and after.** A service `restore` (profile `restore`) checks the whole backup, the versions and an empty target before it changes anything. It creates the roles without a login, runs `pg_restore` in one transaction, copies the files, and then compares the facts of `manifest.sql`, the sequences, the rules of `checks.sql` and the files. A restore that stopped after the database goes on with the files. The facts come from the copy of `manifest.sql` in the backup, so a later change of `manifest.sql` does not break older backups. The guide restores a backup by its name, not by `latest`: a new stack makes an empty backup a minute after its start.
- **A drill.** `deploy/backup/drill.sh` restores the newest backup into a second project (`*-drill`) with no port, beside the running stack. CI runs a drill, a drill with a damaged file, and a restore on a new computer with a new password. The guide drills the chosen backup before the command that deletes the old instance.
- **No secret in a backup.** `.env` and the certificates of Caddy stay out. A copy that leaves the computer is a tar that the owner encrypts with `age`.

## Consequences

- A restore needs no old password: the new `.env` gives one, and the dump has no role passwords.
- The backups lie on the same disk as the data until a copy leaves the computer. A lost laptop takes both. An automatic, encrypted copy in object storage is a later decision, after the hosting (#42).
- Sequences are not part of a snapshot, so the check of a restore cannot compare their values exactly. It compares each one with its value in the dump and with the largest id that it gave.
- Work after the snapshot is lost in a restore: the captures after it come back through an import of a Telegram export with `--since`.
- The fingerprints read each row at each backup. For one owner this takes seconds; at millions of rows it needs counts and samples instead.
- A future delete of a file must keep each file that a kept backup still needs.

## Alternatives considered

- **A copy of the volume `db-data`.** It needs a stopped database for a consistent copy, and it does not move to another major version of Postgres.
- **Continuous archiving of the write-ahead log (pgBackRest, WAL-G).** It restores to any second, but it adds a second program with its own storage and its own failures. One owner does not need that precision.
- **An encrypted copy in object storage first.** It survives a lost laptop, but it needs a bucket, its keys and their storage before the hosting is decided. It is the next step, not the first one.
- **Typed `unshelve backup` commands.** The logic would be Python with types and tests, but the API image has no `pg_dump` 17, and a second copy of the Postgres tools in that image could drift from the server.
