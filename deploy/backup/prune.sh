#!/bin/sh
# Remove the old backups (docs/learn/50). It keeps the newest KEEP_DAILY backups, and the newest
# backup of each of the last KEEP_WEEKLY calendar weeks (Monday to Sunday, in UTC); week 0 is the
# week of the newest backup. A calendar week does not move: the newest backup of a past week
# stays the same backup from one day to the next. The newest backup always stays. It never
# touches the mirror of the files: a file can belong to a backup that stays.
#
#     prune.sh <folder of the backups> <keep daily> <keep weekly>
#
# POSIX sh and awk only, so that the tests run it on Linux and on macOS.
set -eu

dir=$1
daily=$2
weekly=$3

# The names of the complete backups, the newest first: 20261026T090000Z.
names=$(ls "$dir" | grep -E '^[0-9]{8}T[0-9]{6}Z$' | sort -r || true)

printf '%s\n' "$names" | awk -v daily="$daily" -v weekly="$weekly" '
	# The days since 1970-01-01 of a date (the algorithm "days from civil" of Howard Hinnant).
	function days(y, m, d,    era, yoe, doy, doe) {
		y -= (m <= 2)
		era = int(y / 400)
		yoe = y - era * 400
		doy = int((153 * (m + (m > 2 ? -3 : 9)) + 2) / 5) + d - 1
		doe = yoe * 365 + int(yoe / 4) - int(yoe / 100) + doy
		return era * 146097 + doe - 719468
	}
	NF {
		n += 1
		day = days(substr($0, 1, 4) + 0, substr($0, 5, 2) + 0, substr($0, 7, 2) + 0)
		if (n == 1) newest = day
		keep = (n <= daily || n == 1)
		# Day 0, 1970-01-01, was a Thursday: day + 3 counts the weeks from a Monday.
		week = int((newest + 3) / 7) - int((day + 3) / 7)
		if (week < weekly && !(week in seen)) { seen[week] = 1; keep = 1 }
		if (!keep) print
	}
' | while IFS= read -r name; do
	rm -rf "${dir:?}/$name"
done

# A backup that stopped halfway leaves .part-<time>: remove it after a day.
# -mmin, not -mtime: the find of macOS rounds -mtime up, so +0 would take a new part too.
find "$dir" -maxdepth 1 -type d -name '.part-*' -mmin +1440 -exec rm -rf {} +
