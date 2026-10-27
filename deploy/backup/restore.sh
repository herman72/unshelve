#!/usr/bin/env bash
# The restore of a backup of Unshelve (docs/learn/50). The service `restore` runs this script in
# the image of the database, against an empty instance:
#
#     restore.sh list                the complete backups, the oldest first, with their rows
#     restore.sh NAME|latest         check the backup, restore it, and check the data
#     restore.sh check NAME|latest   only the data check, against the database as it is now
#
# Restore a backup by its NAME: "latest" can be a backup that a new, empty stack made a minute ago.
#
# Exit 0 means that the data check passed. Each fact prints "ok: ..." or "not ok: ...".
set -euo pipefail
shopt -s inherit_errexit
umask 077

BACKUPS=${UNSHELVE_BACKUPS:-/backups}
BLOBS=${UNSHELVE_BLOBS:-/var/lib/unshelve/blobs}
MIGRATIONS=${UNSHELVE_MIGRATIONS:-/deploy/migrations}
# The user of the app in deploy/Dockerfile.api: the restored files must be its files.
APP_UID=${UNSHELVE_APP_UID:-10001}
HERE=$(cd "$(dirname "$0")" && pwd)
export PGPASSWORD=${UNSHELVE_DB_PASSWORD:-unshelve}

failures=0
ok() { printf 'ok: %s\n' "$1"; }
not_ok() {
	printf 'not ok: %s\n' "$1"
	failures=$((failures + 1))
}
stop() {
	printf '%s\n' "$1" >&2
	exit 1
}
sql() { psql -X --quiet --no-align --tuples-only -v ON_ERROR_STOP=1 "$@"; }
# psql puts its variables (:'name') into a script on stdin, not into a command of -c.
sql_with() { # sql_with <name> <value> <query>
	sql -v "$1=$2" <<<"$3"
}

backups() {
	[ -d "$BACKUPS/db" ] || return 0
	find "$BACKUPS/db" -mindepth 1 -maxdepth 1 -type d -name '[0-9]*T[0-9]*Z' -printf '%f\n' | sort
}

# One backup for a person: its name, its time in UTC, its items and its files. An empty backup is
# easy to see, and a person restores the backup that holds the data.
describe() { # describe NAME
	local blobs missing
	blobs=$(info_of "$1" blobs)
	missing=$(info_of "$1" missing-blobs)
	printf '%s\tmade %s-%s-%s %s:%s UTC\t%s items\t%s files' "$1" "${1:0:4}" "${1:4:2}" "${1:6:2}" \
		"${1:9:2}" "${1:11:2}" "$(info_of "$1" items)" "$((${blobs:-0} - ${missing:-0}))"
	[ "${missing:-0}" -eq 0 ] || printf ' (%s more lost before the backup)' "$missing"
	printf '\n'
}
info_of() { awk -F'\t' -v key="$2" '$1 == key { print $2; exit }' "$BACKUPS/db/$1/info.txt"; } # info_of NAME KEY

resolve() { # resolve NAME|latest: the name of a complete backup, or stop
	local name=$1
	[ "$name" = latest ] && name=$(backups | tail -n 1)
	[ -n "$name" ] && [ -f "$BACKUPS/db/$name/SHA256SUMS" ] || stop "no complete backup named ${1} in $BACKUPS/db"
	printf '%s\n' "$name"
}

blob_path() { printf '%s/%s/%s/%s\n' "$1" "${2:0:2}" "${2:2:2}" "$2"; }
sha() { sha256sum "$1" | cut -d' ' -f1; }

# The value of a line of info.txt: info <key>
info() { awk -F'\t' -v key="$1" '$1 == key { print $2; exit }' "$dir/info.txt"; }

# The keys of the files that the rows name, without the ones that the store lacked at backup time.
# The lists that the checks read go into files first: a failure in `done < <(...)` would be lost,
# and a check would then pass with fewer facts.
prepare() {
	{ grep -vxFf "$dir/missing-blobs.txt" "$dir/blob-keys.txt" || [ $? -eq 1 ]; } >"$work/needed"
	awk -F'\t' '$1 == "extension"' "$dir/info.txt" >"$work/extensions"
	awk -F'\t' '$1 == "role" && $2 !~ /^pg_/ { print $2 }' "$dir/manifest.tsv" >"$work/roles"
}

# --- a. The backup is whole -----------------------------------------------------------------

check_backup() {
	(cd "$dir" && sha256sum --quiet --check SHA256SUMS >&2) ||
		stop "the backup $name is damaged: a file does not match its checksum. Restore another backup."
	pg_restore --list "$dir/unshelve.dump" >/dev/null ||
		stop "pg_restore cannot read the dump of $name. Restore another backup."
	local key path bad=0
	while IFS= read -r key; do
		path=$(blob_path "$BACKUPS/blobs" "$key")
		if [ ! -f "$path" ] || [ "$(sha "$path")" != "$key" ]; then
			not_ok "blob $key: the mirror lacks it, or holds other bytes"
			bad=$((bad + 1))
		fi
	done <"$work/needed"
	[ "$bad" -eq 0 ] || stop "the mirror of the files is damaged ($bad files): nothing was changed. Each backup shares the mirror, so another backup does not help. If the stack still has its files, remove these files from $BACKUPS/blobs and make a new backup: it copies them again."
	# A role password never comes from a backup: a role that can log in is refused.
	local login
	login=$(awk -F'\t' '$1 == "role" && $3 == "t" { print $2 }' "$dir/manifest.tsv")
	[ -z "$login" ] || stop "the backup names a role that can log in ($login): a restore creates only roles without a login"
	ok "the backup $name is whole: its checksums, its dump and the $(wc -l <"$work/needed") files that it needs"
	echo "the backup: $(describe "$name" | tr '\t' ' ')"
}

# --- b. The versions --------------------------------------------------------------------------

check_versions() {
	local major backup_major revision extension version available
	major=$(sql -c "SELECT current_setting('server_version_num')::int / 10000")
	backup_major=$(info server | cut -d. -f1)
	[ "$major" = "$backup_major" ] ||
		stop "the backup comes from Postgres $backup_major, and this server is Postgres $major"
	revision=$(info revision)
	grep -qxF "revision: str = \"$revision\"" "$MIGRATIONS"/*.py ||
		stop "this checkout does not know the revision $revision of the backup: check out a newer commit"
	while IFS=$'\t' read -r _ extension version; do
		available=$(sql_with name "$extension" "SELECT default_version FROM pg_available_extensions WHERE name = :'name'")
		[ -n "$available" ] && at_least "$available" "$version" ||
			stop "the extension $extension $version of the backup is not available here (${available:-none})"
	done <"$work/extensions"
	ok "the versions: Postgres $major, revision $revision, and the extensions"
}

at_least() { [ "$(printf '%s\n%s\n' "$1" "$2" | sort -V | head -n 1)" = "$2" ]; } # $1 >= $2

# --- c. The target is empty -------------------------------------------------------------------

# The facts come from the manifest.sql that made the backup, when the backup holds a copy: a later
# change of manifest.sql must not make each older backup fail its check.
facts_now() {
	local manifest=$dir/manifest.sql
	[ -f "$manifest" ] || manifest=$HERE/manifest.sql
	psql -X --quiet -v ON_ERROR_STOP=1 -f "$manifest"
}

# Sets `restored`: "no" for an empty database, "yes" when the database already holds this backup
# (an earlier restore stopped after pg_restore, so only the files are left). Anything else stops.
# Not `if check_target`: bash ignores set -e inside the condition of an if.
check_target() {
	local tables strangers
	tables=$(sql -c "SELECT count(*) FROM pg_tables WHERE schemaname IN ('public', 'pub')")
	restored=no
	if [ "$tables" -gt 0 ]; then
		# Other tables can stop manifest.sql (no alembic_version): then the facts differ too.
		facts_now >"$work/facts" 2>/dev/null || true
		cmp -s "$work/facts" "$dir/manifest.tsv" ||
			stop "the database $PGDATABASE is not empty. A restore needs an empty instance: docker compose down --volumes, then docker compose up -d --wait db"
		echo "the database already holds the backup $name: the restore goes on with the files"
		restored=yes
	fi
	# The volume of the files may hold only files of this backup (an earlier restore that stopped).
	strangers=$(find "$BLOBS" -type f ! -name '.part-*' -printf '%f\n' | grep -vxFf "$work/needed" || [ $? -eq 1 ])
	[ -z "$strangers" ] ||
		stop "the volume of the files holds files that are not in the backup $name: it is not empty"
}

# --- d, e. The roles and the database ---------------------------------------------------------

restore_database() {
	local role
	# pg_dump of one database does not create roles, and its GRANTs need them.
	while IFS= read -r role; do
		sql_with role "$role" "SELECT format('CREATE ROLE %I NOLOGIN', :'role')
			WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :'role')" | sql
	done <"$work/roles"
	# One transaction: a failure leaves the database empty, and a second run starts again.
	pg_restore --single-transaction --exit-on-error --no-owner --dbname="$PGDATABASE" "$dir/unshelve.dump"
	# A dump holds no statistics of the planner.
	sql -c "ANALYZE"
	ok "the database: pg_restore of $name, then ANALYZE"
}

# --- f. The files -----------------------------------------------------------------------------

restore_blobs() {
	local key target copied=0
	# What an earlier restore that stopped left.
	find "$BLOBS" -name '.part-*' -delete
	while IFS= read -r key; do
		target=$(blob_path "$BLOBS" "$key")
		[ -f "$target" ] && [ "$(sha "$target")" = "$key" ] && continue
		mkdir -p "${target%/*}"
		# Under a temporary name until the bytes are on the disk: the app never sees half a file.
		cp "$(blob_path "$BACKUPS/blobs" "$key")" "${target%/*}/.part-$key"
		sync "${target%/*}/.part-$key"
		mv "${target%/*}/.part-$key" "$target"
		sync "${target%/*}"
		copied=$((copied + 1))
	done <"$work/needed"
	# The uid only: useradd --system gives the user of the app a group of the system.
	chown -R "$APP_UID" "$BLOBS"
	sync -f "$BLOBS"
	ok "the files: $copied copied into the volume, for the user $APP_UID"
}

# --- g. The data check ------------------------------------------------------------------------

check_data() {
	local line fact seq value called actual
	facts_now >"$work/facts"
	if cmp -s "$dir/manifest.tsv" "$work/facts"; then
		ok "the facts of the database equal the facts of the backup ($(wc -l <"$work/facts") lines)"
	else
		# One line for each fact that differs, by its kind and its name; the whole diff on stderr.
		{ diff "$dir/manifest.tsv" "$work/facts" || true; } | sed -n 's/^[<>] //p' | cut -f1,2 | sort -u >"$work/differ"
		while IFS=$'\t' read -r line fact; do
			not_ok "$line $fact differs from the backup"
		done <"$work/differ"
		diff -u "$dir/manifest.tsv" "$work/facts" >&2 || true
	fi

	# The sequences are not part of a snapshot: each must equal its value in the dump.
	local sequences=0 differ=0
	sequences_of_the_dump >"$work/setvals"
	while read -r seq value called; do
		actual=$(sql -c "SELECT last_value || ' ' || is_called FROM $seq" 2>/dev/null) || actual="missing"
		sequences=$((sequences + 1))
		if [ "$actual" != "$value $called" ]; then
			not_ok "the sequence $seq is at $actual, and the dump says $value $called"
			differ=$((differ + 1))
		fi
	done <"$work/setvals"
	[ "$differ" -gt 0 ] || ok "each of the $sequences sequences has its value of the dump"

	# The rules that are not exact facts. An error stops checks.sql: that is a failure too.
	if ! sql -f "$HERE/checks.sql" >"$work/checks" 2>"$work/checks.error"; then
		not_ok "checks.sql stopped: $(tail -n 1 "$work/checks.error")"
	fi
	while IFS=$'\t' read -r line fact; do
		if [ "$line" = ok ]; then ok "$fact"; else not_ok "$fact"; fi
	done <"$work/checks"

	local extension version installed
	while IFS=$'\t' read -r _ extension version; do
		installed=$(sql_with name "$extension" "SELECT extversion FROM pg_extension WHERE extname = :'name'")
		if [ -n "$installed" ] && at_least "$installed" "$version"; then
			ok "extension $extension $installed (the backup had $version)"
		else
			not_ok "extension $extension is ${installed:-missing}, and the backup had $version"
		fi
	done <"$work/extensions"

	check_blobs
}

sequences_of_the_dump() { # "<sequence> <value> <true|false>" for each setval of the dump
	{ pg_restore --list "$dir/unshelve.dump" | grep ' SEQUENCE SET ' || [ $? -eq 1 ]; } >"$work/sequences"
	if [ -s "$work/sequences" ]; then
		pg_restore --use-list="$work/sequences" --file=- "$dir/unshelve.dump" |
			sed -n "s/^SELECT pg_catalog.setval('\([^']*\)', \([0-9]*\), \(true\|false\));$/\1 \2 \3/p"
	fi
}

check_blobs() {
	local key path size bad=0 wrong_size=0
	while IFS= read -r key; do
		path=$(blob_path "$BLOBS" "$key")
		if [ ! -f "$path" ] || [ "$(sha "$path")" != "$key" ]; then
			not_ok "blob $key: the volume lacks it, or holds other bytes"
			bad=$((bad + 1))
		fi
	done <"$work/needed"
	# The size of each file equals its row in assets.
	sql -F $'\t' -c "SELECT storage_key, size_bytes FROM assets" >"$work/sizes"
	while IFS=$'\t' read -r key size; do
		path=$(blob_path "$BLOBS" "$key")
		if [ -f "$path" ] && [ "$(stat -c %s "$path")" != "$size" ]; then
			not_ok "blob $key has $(stat -c %s "$path") bytes, and assets says $size"
			wrong_size=$((wrong_size + 1))
		fi
	done <"$work/sizes"
	[ "$bad" -eq 0 ] && [ "$wrong_size" -eq 0 ] &&
		ok "the files: each of the $(wc -l <"$work/needed") files is in the volume, with the bytes of its name"
	if [ -n "$(find "$BLOBS" ! -user "$APP_UID" -print -quit)" ]; then
		not_ok "the volume of the files has files that do not belong to the user $APP_UID"
	fi
	if [ -n "$(find "$BLOBS" -name '.part-*' -print -quit)" ]; then
		not_ok "the volume of the files has unfinished files (.part-*)"
	fi
}

finish() {
	if [ "$failures" -gt 0 ]; then
		stop "the data check failed: $failures facts differ from the backup $name"
	fi
	echo "the data check passed: the database and the files equal the backup $name"
}

work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT

case ${1:-} in
list)
	backups | while IFS= read -r name; do describe "$name"; done
	;;
check)
	name=$(resolve "${2:-latest}")
	dir=$BACKUPS/db/$name
	prepare
	check_data
	finish
	;;
'' | -*)
	echo "usage: restore.sh list | NAME|latest | check NAME|latest" >&2
	exit 2
	;;
*)
	name=$(resolve "$1")
	dir=$BACKUPS/db/$name
	prepare
	check_backup
	check_versions
	check_target
	[ "$restored" = yes ] || restore_database
	restore_blobs
	check_data
	finish
	;;
esac
