#!/usr/bin/env bash
# Daily Prospector run: sweep each profile, then embed, then prune.
#
# The user unit prospector-sweep.service runs this script once a day.
# Each step prints a count line. The last line is a total:
#   daily: fetched=N embedded=N pruned=N searched=N failures=N
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
  "$BIN" sweep "$profile" --db "$DB" --time week --combine-terms \
    --max-threads "$MAX_THREADS" --transport "$TRANSPORT" 2>&1 | tee "$LOG"
  if [ "${PIPESTATUS[0]}" -ne 0 ]; then
    echo "sweep $profile FAILED"
    failures=$((failures + 1))
  else
    n="$(value_of fetched)"
    fetched=$((fetched + ${n:-0}))
  fi
done

echo "== embed"
"$BIN" embed --db "$DB" 2>&1 | tee "$LOG"
if [ "${PIPESTATUS[0]}" -ne 0 ]; then
  echo "embed FAILED"
  failures=$((failures + 1))
else
  embedded=$(value_of embedded)
fi

echo "== prune"
"$BIN" prune --db "$DB" --max-age-days "$MAX_AGE_DAYS" --transport "$TRANSPORT" 2>&1 | tee "$LOG"
if [ "${PIPESTATUS[0]}" -ne 0 ]; then
  echo "prune FAILED"
  failures=$((failures + 1))
else
  pruned=$(value_of pruned)
fi

echo "== search check"
"$BIN" semantic-search "$CHECK_QUERY" --db "$DB" --limit 3 2>&1 | tee "$LOG"
if [ "${PIPESTATUS[0]}" -ne 0 ]; then
  echo "search check FAILED"
  failures=$((failures + 1))
else
  searched=$(value_of searched)
fi

echo "daily: fetched=${fetched:-0} embedded=${embedded:-0} pruned=${pruned:-0} searched=${searched:-0} failures=$failures"
[ "$failures" -eq 0 ]
