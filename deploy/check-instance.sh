#!/bin/sh
# Check a running Unshelve as a person uses it: the web address answers, and a sign-in link
# gives a session (docs/quickstart.md). Run it in the folder of the clone, where
# `docker compose` finds the stack:
#
#     deploy/check-instance.sh https://unshelve.example.com
#     deploy/check-instance.sh http://localhost:3000
set -eu

base=${1:?give the address of the web app, for example https://unshelve.example.com}
base=${base%/}

fail() {
	echo "not ok: $*" >&2
	exit 1
}

ready=$(curl -fsS -o /dev/null -D - "$base/api/v1/readyz") || fail "$base/api/v1/readyz does not answer"
echo "ok: $base/api/v1/readyz answers"

# The link goes to stdout, and its note to stderr: the note shows only when there is no link.
note=$(mktemp)
trap 'rm -f "$note"' EXIT
link=$(docker compose exec -T api unshelve auth link 2>"$note") || {
	cat "$note" >&2
	fail "unshelve auth link gave no link"
}
case $link in
"$base/login#token="*) ;;
*) fail "the sign-in link does not start with $base/login#token= (UNSHELVE_WEB_BASE_URL?): $link" ;;
esac
token=${link#*#token=}

headers=$(curl -fsS -o /dev/null -D - -X POST -H "content-type: application/json" \
	--data "{\"token\": \"$token\"}" "$base/api/v1/auth/magic/consume") ||
	fail "the sign-in link was refused"
cookie=$(printf '%s\n' "$headers" | tr -d '\r' | grep -i '^set-cookie: unshelve_session=' | head -n 1)
[ -n "$cookie" ] || fail "the sign-in gave no session cookie"
session=${cookie#*: }
session=${session%%;*}
curl -fsS -o /dev/null -H "cookie: $session" "$base/api/v1/me" || fail "/api/v1/me refused the session"
echo "ok: a sign-in link gives a session"

case $base in
https://*)
	printf '%s\n' "$cookie" | grep -qi '; *secure' || fail "the session cookie has no Secure flag"
	echo "ok: the session cookie is Secure"
	printf '%s\n' "$ready" | tr -d '\r' | grep -qi '^strict-transport-security: max-age=' ||
		fail "the answers have no Strict-Transport-Security header"
	echo "ok: a browser keeps to HTTPS for this name (HSTS)"
	plain=http://${base#https://}
	answer=$(curl -sS -o /dev/null -w '%{http_code} %{redirect_url}' "$plain/") ||
		fail "$plain/ does not answer"
	case $answer in
	30[178]" $base/") echo "ok: $plain/ sends the browser to $base/" ;;
	*) fail "$plain/ answered \"$answer\", not a redirect to $base/" ;;
	esac
	;;
esac
