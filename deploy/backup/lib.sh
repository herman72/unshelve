# shellcheck shell=bash
# The helpers that the scripts of the backup share (docs/learn/50). A script loads them with
#
#     source "$HERE/lib.sh"
#
# This file only defines functions: it sets no shell option and runs nothing when it loads. The
# caller sets "set -euo pipefail" first, as backup.sh does. The helpers use bash and the GNU tools
# of the image of the database (date -d, stat -c, flock).
#
# The variables of the caller that each helper reads:
#
#     log, epoch_of, give_to_owner    none
#     status_get, status_set          STATE, the folder of the status file and of its lock

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

epoch_of() { # epoch_of 20261026T090000Z
	local s=$1
	date -u -d "${s:0:4}-${s:4:2}-${s:6:2}T${s:9:2}:${s:11:2}:${s:13:2}Z" +%s
}

# On Linux the service writes as root: give the new files to the owner of the folder, so that
# the owner can read them without sudo, also after a failure. Docker Desktop shows them as the
# owner's anyway. A file system without owners, such as exFAT, refuses each chown. The first line
# of its error goes into the log, not on stderr: the EXIT trap of backup.sh runs this after the
# line of a failure, and the status keeps the last line of stderr.
give_to_owner() { # give_to_owner <folder whose owner gets the files> <path>...
	local owner error
	owner=$(stat -c %u:%g "$1")
	shift
	if [ "$(id -u)" = 0 ] && [ "$owner" != 0:0 ]; then
		error=$(chown -R "$owner" "$@" 2>&1) || log backup.chown_failed "chown -R $owner: ${error%%$'\n'*}"
	fi
}
