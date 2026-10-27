#!/usr/bin/env bash
# A restore drill (docs/learn/50): restore a backup into a second, empty stack and check its
# data, while the stack of this computer runs on. Then remove the second stack.
#
#     deploy/backup/drill.sh [NAME|latest]
#
# The drill uses the Compose project UNSHELVE_DRILL_PROJECT (unshelve-drill), its own volumes, and
# no port. It reads the backups of UNSHELVE_BACKUP_DIR (backups/ of the clone) and never writes
# them. A new random password for its database shows that a restore needs no old password.
set -euo pipefail

project=${UNSHELVE_DRILL_PROJECT:-unshelve-drill}
# A drill removes the volumes of its project at the end: never those of the real stack.
case $project in
*-drill) ;;
*)
	echo "the project of a drill must end with -drill, not \"$project\"" >&2
	exit 2
	;;
esac

here=$(cd "$(dirname "$0")" && pwd)
root=$(cd "$here/../.." && pwd)
# The folder of the backups, as the stack finds it: the shell first, then .env (the last line of a
# name counts), then backups/ of the clone. A relative path counts from deploy/, as in Compose.
backups=${UNSHELVE_BACKUP_DIR:-}
if [ -z "$backups" ] && [ -f "$root/.env" ]; then
	backups=$(sed -n 's/^UNSHELVE_BACKUP_DIR=//p' "$root/.env" | tail -n 1 | sed "s/^[\"']//; s/[\"']$//")
fi
backups=${backups:-$root/backups}
case $backups in
/*) ;;
*) backups=$root/deploy/$backups ;;
esac
if [ ! -d "$backups/db" ]; then
	echo "no backups in $backups (UNSHELVE_BACKUP_DIR)" >&2
	exit 1
fi
export UNSHELVE_BACKUP_DIR=$backups
UNSHELVE_DB_PASSWORD=$(od -An -N16 -tx1 /dev/urandom | tr -d ' \n')
export UNSHELVE_DB_PASSWORD

compose() {
	docker compose -p "$project" -f "$root/deploy/compose.yaml" -f "$here/drill.yaml" \
		--profile restore "$@"
}

trap 'compose down --volumes >/dev/null 2>&1 || true' EXIT
# A drill starts empty, also after a drill that a stop cut short.
compose down --volumes >/dev/null 2>&1 || true
compose up -d --wait db
compose run --rm restore "${1:-latest}"
