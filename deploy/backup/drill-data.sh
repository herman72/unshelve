#!/usr/bin/env bash
# Data for the restore in CI (.github/workflows/stack.yml, job quickstart-local): an import of the
# made-up export of the tests, with two photos and a PDF, so that the backup holds rows of each
# kind and files. Then it waits until the pipeline ends with each item. Not for a real instance.
set -euo pipefail

root=$(cd "$(dirname "$0")/../.." && pwd)
export_folder=$root/exports/drill
rm -rf "$export_folder"
cp -R "$root/apps/api/tests/ingest/fixtures/export_made_up" "$export_folder"
mkdir -p "$export_folder/photos" "$export_folder/files"
head -c 4096 /dev/urandom >"$export_folder/photos/photo_1@13-10-2026_23-25-17.jpg"
head -c 4096 /dev/urandom >"$export_folder/photos/photo_2@13-10-2026_23-25-17.jpg"
head -c 8192 /dev/urandom >"$export_folder/files/part-one.pdf"

docker compose exec -T api unshelve import /imports/drill --timezone Europe/Helsinki --chat-id -1001

# The worker makes the items from the jobs of the import: wait until no job is ready or running,
# and the items exist. A job that waits for later (an album waits 5 seconds) can stay pending:
# the restored worker does it, so CI compares the rows after the restore with "at least".
settled() {
	docker compose exec -T db psql -U unshelve -d unshelve -At -c "SELECT
		NOT EXISTS (SELECT 1 FROM jobs WHERE state = 'running'
			OR (state = 'pending' AND run_after <= now()))
		AND EXISTS (SELECT 1 FROM items)"
}
for _ in $(seq 90); do
	[ "$(settled)" = t ] && exit 0
	sleep 2
done
echo "the pipeline did not end with each item in 3 minutes" >&2
exit 1
