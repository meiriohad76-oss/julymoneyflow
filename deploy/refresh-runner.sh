#!/usr/bin/env bash
# Consume a queued refresh request and run the pipeline.
#
# Started by smf-trigger.path when /opt/smf/run/refresh.request appears. The web
# server writes that file; it never runs anything itself. This is the whole point
# of the split: the server stays sandboxed with no network, no write access to
# data/ or output/, and no ability to execute, while this unit gets exactly the
# permissions the pipeline needs and nothing more.
#
# The request file contains one word: "offline" or "fetch". It is validated here
# against a fixed set rather than trusted, because it arrives from an HTTP
# handler, and is never interpolated into a command.

set -uo pipefail

# Overridable so the logic can be exercised outside a real install.
APP=${SMF_APP:-/opt/smf}
RUNDIR=$APP/run
REQ=$RUNDIR/refresh.request
RUNNING=$RUNDIR/running
LASTFETCH=$RUNDIR/last_fetch
PY=$APP/.venv/bin/python

[[ -f "$REQ" ]] || { echo "no request file; nothing to do"; exit 0; }

MODE=$(head -c 32 "$REQ" | tr -dc 'a-z' || true)
# Remove the request immediately. If the pipeline dies, a stale request must not
# cause it to be retried in a loop by the .path unit.
rm -f "$REQ"

case "$MODE" in
  offline|fetch) ;;
  *) echo "invalid mode '$MODE'; refusing"; exit 1 ;;
esac

if [[ -f "$RUNNING" ]]; then
  echo "a refresh is already running; skipping"
  exit 0
fi

# Marker so /status can report progress, cleared on every exit path.
echo "$MODE $(date -Is)" > "$RUNNING"
trap 'rm -f "$RUNNING"' EXIT

echo "==> refresh mode=$MODE"
if [[ "$MODE" == "offline" ]]; then
  # Rebuilds from the existing cache. No provider requests, no quota spent.
  "$PY" "$APP/run.py" --offline
  rc=$?
else
  # Spends Polygon quota. Stamp the time BEFORE running so a crash mid-fetch
  # still starts the cooldown -- a failing fetch that could be retried instantly
  # is the expensive failure mode.
  date +%s > "$LASTFETCH"
  "$PY" "$APP/run.py"
  rc=$?
fi

echo "==> refresh finished rc=$rc"
exit $rc
