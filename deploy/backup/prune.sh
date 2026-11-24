#!/bin/sh
# Remove the old backups (docs/learn/50). It keeps the newest KEEP_DAILY backups, and the newest
# backup of each of the last KEEP_WEEKLY calendar weeks (Monday to Sunday, in UTC); week 0 is the
# week of the newest backup. A calendar week does not move: the newest backup of a past week
# stays the same backup from one day to the next. The newest backup always stays. It never
# touches the mirror of the files: a file can belong to a backup that stays.
#
# The pin: the newest backup with items always stays too. Its info.txt has the line
# "items<TAB>N", and N is more than 0. N is the rest of the line after the TAB. When an info.txt
# has two lines named items, the first one counts.
# When a newer backup has items, that backup becomes the pin, and the old pin goes by its age.
# Without a backup with items, there is no pin.
#
# Why: the database can become empty, and nobody sees it. Then each new backup is empty. After
# KEEP_DAILY new backups, a rule of age alone removes the newest backup with items. A backup stays
# longer only when it is the newest backup of its calendar week. With 7 and 6, no backup with
# items is left after 30 to 36 days.
#
# The walk for the pin reads the info.txt of each backup, from the newest backup to the oldest.
# It stops at the pin, and it never reads an older backup. A sync tool can take the bytes of an
# old file off the disk. The prune must not stop for such a file.
#
# Fail closed: when the walk cannot get the count of items of a backup, the prune removes nothing,
# also no old part. It prints one line on stderr that names the file, and exits 1. The walk
# cannot get the count in these cases (the text of the line in quotes):
#
#   - no info.txt: "no file"
#   - an info.txt that the prune cannot read: "cannot read"
#   - no line "items": "no count of items in"
#   - a value that is not a whole number, also with a second TAB: "no count of items in"
#
# On Docker Desktop, an info.txt without the right to read, or a folder without the rights to
# read and open, also gives "no file". A count that the prune cannot get is not 0 items: that
# backup can be the newest backup with items. The note docs/learn/50 tells what each line
# means, and how a person repairs the backup.
#
#     prune.sh <folder of the backups> <keep daily> <keep weekly>
#
# POSIX sh and awk only, so that the tests run it on Linux and on macOS.
set -eu

dir=$1
daily=$2
weekly=$3

refuse() { # refuse <reason>: one line on stderr, and nothing is removed
	printf 'prune.sh removed nothing: %s\n' "$1" >&2
	exit 1
}

# The names of the complete backups, the newest first: 20261026T090000Z.
names=$(ls "$dir" | grep -E '^[0-9]{8}T[0-9]{6}Z$' | sort -r || true)

pin=""
for name in $names; do
	file=$dir/$name/info.txt
	[ -f "$file" ] || refuse "no file $file"
	# The value of the first line "items". "-" when no line has that name, or when that line has
	# a second TAB.
	items=$(awk -F'\t' '
		$1 == "items" { print (NF == 2 ? $2 : "-"); found = 1; exit }
		END { if (!found) print "-" }
	' "$file" 2>/dev/null) || refuse "cannot read $file"
	case $items in
	'' | *[!0-9]*) refuse "no count of items in $file" ;;
	esac
	# A whole number is more than 0 when it has a digit other than 0.
	case $items in
	*[1-9]*)
		pin=$name
		break
		;;
	esac
done

printf '%s\n' "$names" | awk -v daily="$daily" -v weekly="$weekly" -v pin="$pin" '
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
		keep = (n <= daily || n == 1 || $0 == pin)
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
