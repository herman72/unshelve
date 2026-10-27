#!/usr/bin/env bash
# The backups of Unshelve (docs/learn/50). The service `backup` runs this script in the image of
# the database, so pg_dump always has the major version of the server.
#
#     backup.sh loop           a backup when the newest one is older than UNSHELVE_BACKUP_EVERY_HOURS
#     backup.sh once           one backup now; it waits while another one runs
#     backup.sh health         exit 1 when the last run failed, or the newest backup is too old
#     backup.sh status         the state of the backups, for a person
#     backup.sh export NAME    one backup and the files that it needs, as a tar on stdout
#                              (NAME "latest" is the newest backup)
#
# The folder /backups holds db/<UTC time>/ for each backup, and blobs/, one mirror of the files
# that users sent: a file name is the SHA-256 of its bytes, so a file never changes.
set -euo pipefail
# A command substitution stops on an error too, as the rest of the script does.
shopt -s inherit_errexit
umask 077

BACKUPS=${UNSHELVE_BACKUPS:-/backups}
BLOBS=${UNSHELVE_BLOBS:-/var/lib/unshelve/blobs}
STATE=${UNSHELVE_BACKUP_STATE:-/var/lib/unshelve/backup-status}
EVERY_HOURS=${UNSHELVE_BACKUP_EVERY_HOURS:-24}
KEEP_DAILY=${UNSHELVE_BACKUP_KEEP_DAILY:-7}
KEEP_WEEKLY=${UNSHELVE_BACKUP_KEEP_WEEKLY:-6}
RETRY_MINUTES=${UNSHELVE_BACKUP_RETRY_MINUTES:-60}
HERE=$(cd "$(dirname "$0")" && pwd)
# The password of the database from .env; without it, the default of deploy/compose.yaml.
export PGPASSWORD=${UNSHELVE_DB_PASSWORD:-unshelve}
# The lock lies on the volume of the status, a file system of Docker. On a shared folder of
# Docker Desktop, a lock taken by the same call that created the file keeps nobody out.
LOCK=$STATE/backup.lock

# Whole numbers without a leading zero: bash reads 010 as the octal number 8.
for setting in EVERY_HOURS KEEP_DAILY KEEP_WEEKLY RETRY_MINUTES; do
	case ${!setting} in
	'' | *[!0-9]* | 0?*)
		echo "UNSHELVE_BACKUP_$setting must be a whole number without a leading zero, not \"${!setting}\"" >&2
		exit 2
		;;
	esac
done
# KEEP_DAILY=0 would remove each backup, also the new one.
for setting in EVERY_HOURS KEEP_DAILY; do
	if [ "${!setting}" -lt 1 ]; then
		echo "UNSHELVE_BACKUP_$setting must be 1 or more, not ${!setting}" >&2
		exit 2
	fi
done

log() { # one JSON line for docker compose logs: log <event> <text>
	printf '{"event": "%s", "time": "%s", "detail": "%s"}\n' "$1" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
		"$(printf '%s' "$2" | tr '\n"\\' "  '")"
}

# The status file holds key=value lines, and no secret.
status_get() { [ -f "$STATE/status" ] && sed -n "s/^$1=//p" "$STATE/status" | tail -n 1 || true; }
status_set() { # status_set key value [key value ...]: one change of the file, under its own lock
	mkdir -p "$STATE"
	(
		flock 8
		local file=$STATE/status tmp pattern="" i value
		tmp=$(mktemp "$STATE/status.XXXXXX")
		for ((i = 1; i < $#; i += 2)); do pattern+="${pattern:+|}^${!i}="; done
		{ [ -f "$file" ] && grep -Ev "$pattern" "$file" || true; } >"$tmp"
		for ((i = 1; i < $#; i += 2)); do
			value=$((i + 1))
			printf '%s=%s\n' "${!i}" "${!value}" >>"$tmp"
		done
		mv "$tmp" "$file"
	) 8>>"$STATE/status.lock"
}

# The names of the complete backups, the oldest first. A name is its UTC time: 20261026T090000Z.
backups() {
	[ -d "$BACKUPS/db" ] || return 0
	find "$BACKUPS/db" -mindepth 1 -maxdepth 1 -type d -name '[0-9]*T[0-9]*Z' -printf '%f\n' | sort
}

# A clock that was ahead gives a name in the future. Such a backup is not the newest for the
# schedule, or no backup would be due until the clock reaches its name.
newest() { backups | awk -v limit="$(soon)" '$0 <= limit' | tail -n 1; }
in_future() { backups | awk -v limit="$(soon)" '$0 > limit' | head -n 1; }
soon() { date -u -d "@$(($(date -u +%s) + 300))" +%Y%m%dT%H%M%SZ; }

epoch_of() { # epoch_of 20261026T090000Z
	local s=$1
	date -u -d "${s:0:4}-${s:4:2}-${s:6:2}T${s:9:2}:${s:11:2}:${s:13:2}Z" +%s
}

resolve() { # resolve NAME|latest: the name of a complete backup, or exit
	local name=$1
	[ "$name" = latest ] && name=$(backups | tail -n 1)
	[ -n "$name" ] && [ -f "$BACKUPS/db/$name/SHA256SUMS" ] || {
		echo "no complete backup named ${1}" >&2
		exit 1
	}
	printf '%s\n' "$name"
}

blob_path() { printf '%s/%s/%s/%s\n' "$1" "${2:0:2}" "${2:2:2}" "$2"; }

# Each file of the live store, as "<size> <path>".
live_blobs() {
	[ -d "$BLOBS" ] || return 0
	find "$BLOBS" -type f -regextype posix-extended -regex '.*/[0-9a-f]{64}' -printf '%s %p\n'
}

# A mirror copy with the size of its file in the store stays. Another size means a copy that a
# crash cut short, so it is copied again.
mirrored() { # mirrored <size> <key>
	local target
	target=$(blob_path "$BACKUPS/blobs" "$2")
	[ -f "$target" ] && [ "$(stat -c %s "$target")" = "$1" ]
}

new_blobs_kb() { # the size of the files that the mirror must copy, in KB
	local size path total=0
	while read -r size path; do
		mirrored "$size" "${path##*/}" || total=$((total + size))
	done < <(live_blobs)
	echo $(((total + 1023) / 1024))
}

# Copy each file of the live store that the mirror lacks. A copy is checked against its name,
# and written to the disk, before it gets that name: the mirror never holds a wrong file.
mirror_blobs() {
	local size path key target copied=0
	while read -r size path; do
		key=${path##*/}
		mirrored "$size" "$key" && continue
		target=$(blob_path "$BACKUPS/blobs" "$key")
		mkdir -p "${target%/*}"
		cp "$path" "$target.part"
		if [ "$(sha256sum "$target.part" | cut -d' ' -f1)" != "$key" ]; then
			rm -f "$target.part"
			echo "the file $key in the store does not have the bytes of its name" >&2
			return 1
		fi
		sync "$target.part"
		mv "$target.part" "$target"
		sync "${target%/*}"
		copied=$((copied + 1))
	done < <(live_blobs)
	echo "$copied"
}

# What a backup that stopped left: only one backup runs at a time, so each part is old.
clean_parts() {
	rm -rf "$BACKUPS"/db/.part-*
	find "$BACKUPS/blobs" -type f -name '*.part' -delete
}

wait_for_database() { # after a cold start of Docker, the database can need a minute
	local tries=0
	until pg_isready --quiet; do
		tries=$((tries + 1))
		if [ "$tries" -ge 60 ]; then
			echo "the database at $PGHOST did not answer for 2 minutes" >&2
			return 1
		fi
		sleep 2
	done
}

# On Linux the service writes as root: give the new files to the owner of the folder, so that
# the owner can read them without sudo, also after a failure. Docker Desktop shows them as the
# owner's anyway.
give_to_owner() {
	local owner
	owner=$(stat -c %u:%g "$BACKUPS")
	if [ "$(id -u)" = 0 ] && [ "$owner" != 0:0 ]; then
		chown -R "$owner" "$BACKUPS/db" "$BACKUPS/blobs" "$BACKUPS/.gitignore" || true
	fi
}

work=""
once() {
	mkdir -p "$BACKUPS/db" "$BACKUPS/blobs" "$STATE"
	# A folder of backups never goes into git, also when it lies inside a clone.
	[ -f "$BACKUPS/.gitignore" ] || printf '*\n' >"$BACKUPS/.gitignore"
	exec 9>>"$LOCK"
	flock 9
	# A stop or a failure removes the part of this backup: a lasting failure must not fill the disk.
	trap 'exit 143' TERM INT
	trap '[ -n "$work" ] && rm -rf "$work"; find "$BACKUPS/blobs" -type f -name "*.part" -delete || true; give_to_owner' EXIT
	clean_parts
	wait_for_database

	# Room for two dumps of the last size, the files that the mirror lacks, and 100 MB.
	local last need free
	last=$(backups | tail -n 1)
	need=$((100 * 1024 + $(new_blobs_kb)))
	if [ -n "$last" ]; then
		need=$((need + 2 * $(du -sk "$BACKUPS/db/$last/unshelve.dump" | cut -f1)))
	fi
	free=$(df -Pk "$BACKUPS" | awk 'NR == 2 {print $4}')
	if [ "$free" -lt "$need" ]; then
		echo "only ${free} KB are free in the backup folder, and a backup needs ${need} KB" >&2
		return 1
	fi

	# A name is a second: a backup in a taken second waits for the next one. Any other failure of
	# mkdir, such as a disk that became read-only, stops the backup with its message.
	local name
	name=$(date -u +%Y%m%dT%H%M%SZ)
	while [ -e "$BACKUPS/db/$name" ] || [ -e "$BACKUPS/db/.part-$name" ]; do
		sleep 1
		name=$(date -u +%Y%m%dT%H%M%SZ)
	done
	mkdir "$BACKUPS/db/.part-$name"
	work=$BACKUPS/db/.part-$name
	(cd "$work" && psql -X --quiet -v ON_ERROR_STOP=1 -f "$HERE/snapshot.sql" >/dev/null)
	[ -f "$work/dump.ok" ] || {
		echo "pg_dump did not finish" >&2
		return 1
	}
	rm "$work/dump.ok"
	pg_restore --list "$work/unshelve.dump" >/dev/null

	local copied key missing=0
	copied=$(mirror_blobs)
	# A key that the rows name but the live store lacks was missing before this backup: the
	# app logs files.copy_missing for it. The backup notes it and goes on.
	: >"$work/missing-blobs.txt"
	while IFS= read -r key; do
		if [ ! -f "$(blob_path "$BACKUPS/blobs" "$key")" ]; then
			printf '%s\n' "$key" >>"$work/missing-blobs.txt"
			missing=$((missing + 1))
		fi
	done <"$work/blob-keys.txt"
	{
		echo "dump	$(pg_dump --version)"
		echo "blobs	$(wc -l <"$work/blob-keys.txt")"
		echo "missing-blobs	$missing"
	} >>"$work/info.txt"
	# The manifest.sql that wrote the facts: a restore runs it again, also after a later change.
	cp "$HERE/manifest.sql" "$work/manifest.sql"
	(cd "$work" && sha256sum unshelve.dump manifest.tsv manifest.sql blob-keys.txt missing-blobs.txt info.txt >SHA256SUMS)
	# On the disk before the name, so that a crash never leaves a complete name with lost bytes.
	sync "$work"/* "$work"
	mv "$work" "$BACKUPS/db/$name"
	work=""
	sync "$BACKUPS/db"

	"$HERE/prune.sh" "$BACKUPS/db" "$KEEP_DAILY" "$KEEP_WEEKLY"
	local size
	size=$(du -sk "$BACKUPS/db/$name/unshelve.dump" | cut -f1)
	status_set last_ok "$(date -u +%s)" last_name "$name" last_dump_kb "$size" \
		last_blobs_copied "$copied" last_missing_blobs "$missing"
	log backup.ok "$name: dump ${size} KB; files copied: ${copied}; files that the store already lacked: ${missing}"
}

child=""
run_once() { # once, in a process of its own, and the status of a failure
	# Not `if once`: bash ignores set -e inside the condition of an if, so a failed step would
	# not stop the backup. A new process of this script has its own set -e.
	local errors code=0
	errors=$(mktemp)
	bash "$HERE/backup.sh" _once 2>"$errors" &
	child=$!
	wait "$child" || code=$?
	child=""
	if [ "$code" -eq 0 ]; then
		rm -f "$errors"
		return 0
	fi
	status_set last_error "$(date -u +%s)" last_error_text "$(tail -n 1 "$errors")"
	log backup.failed "$(cat "$errors")"
	cat "$errors" >&2
	rm -f "$errors"
	return 1
}

due() { # a backup is due when the newest one is older than EVERY_HOURS
	local name
	name=$(newest)
	[ -z "$name" ] && return 0
	[ $(($(date -u +%s) - $(epoch_of "$name"))) -ge $((EVERY_HOURS * 3600)) ]
}

recently_failed() {
	local error ok
	error=$(status_get last_error)
	ok=$(status_get last_ok)
	[ -n "$error" ] && [ "$error" -gt "${ok:-0}" ] && [ $(($(date -u +%s) - error)) -lt $((RETRY_MINUTES * 60)) ]
}

tick() { # one round of the loop
	# A computer that slept through the time makes the backup when it wakes. While a backup
	# that a person started holds the lock, this round waits for the next one.
	if due && ! recently_failed && flock -n "$LOCK" true; then
		run_once || true
	fi
}

stop() { # docker compose stop: the backup that runs removes its part, then the loop ends
	if [ -n "$child" ]; then
		kill -TERM "$child" 2>/dev/null || true
		wait "$child" 2>/dev/null || true
	fi
	exit 0
}

loop() {
	mkdir -p "$BACKUPS" "$STATE"
	: >>"$LOCK"
	trap stop TERM INT
	status_set started "$(date -u +%s)"
	log backup.started "a backup when the newest is older than $EVERY_HOURS hours"
	while true; do
		tick
		sleep 300 &
		wait $! || true
	done
}

health() {
	local error ok started name future now limit
	error=$(status_get last_error)
	ok=$(status_get last_ok)
	if [ -n "$error" ] && [ "$error" -gt "${ok:-0}" ]; then
		echo "the last backup failed: $(status_get last_error_text)"
		exit 1
	fi
	future=$(in_future)
	if [ -n "$future" ]; then
		echo "the backup $future has a name in the future: check the clock, then remove it"
		exit 1
	fi
	# After a start, the first round has an hour to make a backup. Then the newest backup must
	# be younger than two periods, also after a restart of the service.
	now=$(date -u +%s)
	limit=$((2 * EVERY_HOURS * 3600))
	started=$(status_get started)
	name=$(newest)
	if [ -n "$started" ] && [ $((now - started)) -gt 3600 ]; then
		if [ -z "$name" ] || [ $((now - $(epoch_of "$name"))) -gt "$limit" ]; then
			echo "no backup in $((2 * EVERY_HOURS)) hours"
			exit 1
		fi
	fi
	echo "ok"
}

when() { # when <epoch>: a UTC time and its age, for a person
	local hours=$((($(date -u +%s) - $1) / 3600))
	printf '%s UTC, %s hours ago\n' "$(date -u -d "@$1" '+%Y-%m-%d %H:%M')" "$hours"
}

show_status() {
	local name count ok error
	count=$(backups | wc -l)
	echo "backups: $count in $BACKUPS/db"
	if [ "$count" -gt 0 ]; then
		name=$(backups | tail -n 1)
		echo "newest: $name, $(du -sh "$BACKUPS/db/$name" | cut -f1)"
		sed 's/^/  /' "$BACKUPS/db/$name/info.txt"
	fi
	echo "files in the mirror: $(find "$BACKUPS/blobs" -type f 2>/dev/null | wc -l)"
	ok=$(status_get last_ok)
	error=$(status_get last_error)
	echo "last success: $([ -n "$ok" ] && when "$ok" || echo none)"
	echo "last failure: $([ -n "$error" ] && echo "$(when "$error"): $(status_get last_error_text)" || echo none)"
	echo "health: $(health || true)"
}

export_tar() { # one backup and each file that it needs, as a tar on stdout
	local name list
	name=$(resolve "${1:?give the name of a backup, or latest}")
	# A damaged backup gives no tar: each file must match its checksum.
	(cd "$BACKUPS/db/$name" && sha256sum --quiet --check SHA256SUMS >&2)
	list=$(mktemp)
	trap 'rm -f "$list"' RETURN
	printf 'db/%s\n' "$name" >"$list"
	# grep ends with 1 when it keeps no line (each file is missing); 2 is an error.
	{ grep -vxFf "$BACKUPS/db/$name/missing-blobs.txt" "$BACKUPS/db/$name/blob-keys.txt" || [ $? -eq 1 ]; } |
		while IFS= read -r key; do blob_path blobs "$key"; done >>"$list"
	tar -C "$BACKUPS" -cf - -T "$list"
}

case ${1:-} in
once) run_once ;;
loop) loop ;;
health) health ;;
status) show_status ;;
export) export_tar "${2:-}" ;;
_once) once ;;  # the backup itself, in the process that run_once starts
_due) due ;;    # for the tests: exit 0 when a backup is due
_tick) tick ;;  # for the tests: one round of the loop
_manifest) psql -X --quiet -v ON_ERROR_STOP=1 -f "$HERE/manifest.sql" ;; # the facts now
*)
	echo "usage: backup.sh once|loop|health|status|export NAME" >&2
	exit 2
	;;
esac
