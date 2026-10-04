#!/usr/bin/env bash
# Daily Prospector run: sweep each profile, then embed, then prune.
#
# The user unit prospector-sweep.service runs this script once a day.
# Each step prints a count line. The last line is a total:
#   daily: fetched=N embedded=N pruned=N searched=N profiles_ok=N profiles_failed=N failures=N
#
# A profile counts as failed when its sweep exits non-zero, fetches nothing,
# or gets more refused requests (403 + 429) than good ones.
# The script exits non-zero when every profile failed, or when the embed,
# prune or search-check step failed. One failed profile alone is reported in
# the daily line but does not fail the unit.
#
# The prune step always runs, also when a sweep or the embed step fails,
# because content deleted on Reddit must leave the store at every run.
#
# Settings (environment, all optional):
#   PROSPECTOR_HOME           checkout with .venv        (default ~/prospector)
#   PROSPECTOR_DATA           store, cache, profiles     (default ~/prospector-data)
#   PROSPECTOR_SWEEP_PROFILES profiles to sweep          (default "hospital-tech saas-pain")
#   PROSPECTOR_MAX_THREADS    comment threads per profile (default 15)
#   PROSPECTOR_MAX_AGE_DAYS   retention for prune        (default 7)
#   PROSPECTOR_TRANSPORT      auto | json | rss          (default auto)
#   PROSPECTOR_CHECK_QUERY    query for the final search check
set -u -o pipefail

APP_DIR="${PROSPECTOR_HOME:-$HOME/prospector}"
DATA_DIR="${PROSPECTOR_DATA:-$HOME/prospector-data}"
DB="$DATA_DIR/prospector.db"
BIN="$APP_DIR/.venv/bin/prospector"
PROFILES="${PROSPECTOR_SWEEP_PROFILES:-hospital-tech saas-pain}"
MAX_THREADS="${PROSPECTOR_MAX_THREADS:-15}"
MAX_AGE_DAYS="${PROSPECTOR_MAX_AGE_DAYS:-7}"
TRANSPORT="${PROSPECTOR_TRANSPORT:-auto}"
CHECK_QUERY="${PROSPECTOR_CHECK_QUERY:-is there a tool that does this for me}"
export PROSPECTOR_CACHE_DIR="${PROSPECTOR_CACHE_DIR:-$DATA_DIR/cache}"

mkdir -p "$DATA_DIR" "$PROSPECTOR_CACHE_DIR"
LOG="$(mktemp)"
trap 'rm -f "$LOG"' EXIT

failures=0
step_failures=0
profiles_ok=0
profiles_failed=0
fetched=0
embedded=0
pruned=0
searched=0

# Print the value of key=VALUE from the last matching line of $LOG.
value_of() {
  grep -o "$1=[0-9]*" "$LOG" | tail -n 1 | cut -d= -f2
}

echo "prospector daily: db=$DB profiles=[$PROFILES] transport=$TRANSPORT"

for profile in $PROFILES; do
  echo "== sweep $profile"
  : > "$LOG"
  "$BIN" sweep "$profile" --db "$DB" --time week --combine-terms \
    --max-threads "$MAX_THREADS" --transport "$TRANSPORT" 2>&1 | tee "$LOG"
  rc="${PIPESTATUS[0]}"
  n="$(value_of fetched)"; n="${n:-0}"
  ok="$(value_of requests_ok)"; ok="${ok:-0}"
  r403="$(value_of requests_403)"; r429="$(value_of requests_429)"
  refused=$(( ${r403:-0} + ${r429:-0} ))
  fetched=$((fetched + n))
  reason=""
  if [ "$rc" -ne 0 ]; then
    reason="exit code $rc"
  elif [ "$n" -eq 0 ]; then
    reason="fetched nothing"
  elif [ "$refused" -gt "$ok" ]; then
    reason="most requests refused ($refused refused, $ok ok)"
  fi
  if [ -n "$reason" ]; then
    echo "sweep $profile FAILED: $reason"
    profiles_failed=$((profiles_failed + 1))
  else
    profiles_ok=$((profiles_ok + 1))
  fi
done

echo "== embed"
: > "$LOG"
"$BIN" embed --db "$DB" 2>&1 | tee "$LOG"
if [ "${PIPESTATUS[0]}" -ne 0 ]; then
  echo "embed FAILED"
  step_failures=$((step_failures + 1))
else
  embedded=$(value_of embedded)
fi

echo "== prune"
: > "$LOG"
"$BIN" prune --db "$DB" --max-age-days "$MAX_AGE_DAYS" --transport "$TRANSPORT" 2>&1 | tee "$LOG"
if [ "${PIPESTATUS[0]}" -ne 0 ]; then
  echo "prune FAILED"
  step_failures=$((step_failures + 1))
else
  pruned=$(value_of pruned)
fi

echo "== search check"
: > "$LOG"
"$BIN" semantic-search "$CHECK_QUERY" --db "$DB" --limit 3 2>&1 | tee "$LOG"
if [ "${PIPESTATUS[0]}" -ne 0 ]; then
  echo "search check FAILED"
  step_failures=$((step_failures + 1))
else
  searched=$(value_of searched)
fi

failures=$((profiles_failed + step_failures))
echo "daily: fetched=${fetched:-0} embedded=${embedded:-0} pruned=${pruned:-0} searched=${searched:-0} profiles_ok=$profiles_ok profiles_failed=$profiles_failed failures=$failures"
[ "$profiles_ok" -gt 0 ] && [ "$step_failures" -eq 0 ]
