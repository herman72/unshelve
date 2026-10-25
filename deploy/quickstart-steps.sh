#!/bin/sh
# Print the shell blocks of docs/quickstart.md that carry one mark, in their order, so that CI
# runs the steps exactly as the guide shows them:
#
#     deploy/quickstart-steps.sh quickstart-server | bash -euxo pipefail
#
# A block carries a mark after its language, as in ```bash quickstart-server, and GitHub shows
# it as an ordinary bash block. A block inside a list is indented: like GitHub, this script
# removes that indentation, so a here-document ends at the start of a line.
set -eu

mark=${1:?give a mark, for example quickstart-server}
guide=$(dirname "$0")/../docs/quickstart.md
fence='```'
steps=$(awk -v opening="${fence}bash $mark" -v closing="$fence" '
	!inside {
		match($0, /^ */)
		if (substr($0, RLENGTH + 1) == opening) { inside = 1; indent = RLENGTH }
		next
	}
	{
		line = $0
		rest = line
		sub(/^ */, "", rest)
		if (rest == closing) { inside = 0; next }
		n = 0
		while (n < indent && substr(line, n + 1, 1) == " ") n++
		print substr(line, n + 1)
	}
' "$guide")
[ -n "$steps" ] || {
	echo "docs/quickstart.md has no block marked $mark" >&2
	exit 1
}
printf '%s\n' "$steps"
