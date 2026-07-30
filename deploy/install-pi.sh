#!/usr/bin/env bash
# Smart Money Flow — Raspberry Pi installer
#
# Idempotent: safe to re-run after editing anything. Does NOT install cloudflared
# credentials (that requires an interactive browser login — see README.md step 4).
#
#   sudo bash deploy/install-pi.sh
#
# Assumes Raspberry Pi OS Bookworm (64-bit) or Ubuntu on arm64. 64-bit matters:
# piwheels ships prebuilt numpy/pandas/scipy wheels for aarch64, and on 32-bit
# you would compile them from source for well over an hour.

set -euo pipefail

APP_DIR=/opt/smf
APP_USER=smf
PORT=8080

say() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[33m  ! %s\033[0m\n' "$*"; }

[[ $EUID -eq 0 ]] || { echo "run with sudo"; exit 1; }

say "Checking architecture"
ARCH=$(uname -m)
echo "  $ARCH"
if [[ "$ARCH" != "aarch64" && "$ARCH" != "x86_64" ]]; then
  warn "32-bit detected. numpy/pandas/scipy have no prebuilt wheels here and will"
  warn "compile from source (1h+ on a Pi). A 64-bit OS is strongly recommended."
  read -rp "  continue anyway? [y/N] " ok; [[ "$ok" == "y" ]] || exit 1
fi

say "Installing system packages"
apt-get update -qq
# python3-venv for the venv; the -dev headers only matter if a wheel is missing.
apt-get install -y -qq python3 python3-venv python3-dev build-essential \
                       ca-certificates curl rsync >/dev/null
echo "  done"

say "Creating service account '$APP_USER'"
if ! id -u "$APP_USER" >/dev/null 2>&1; then
  # No login shell, no home directory content, no password. This account exists
  # only to own the files and run two units.
  useradd --system --create-home --home-dir "$APP_DIR" --shell /usr/sbin/nologin "$APP_USER"
  echo "  created"
else
  echo "  already exists"
fi
# run/ is the only directory the web server may write to; it holds the refresh
# request flag. See deploy/serve.py and smf-trigger.path.
mkdir -p "$APP_DIR"/{data,output,deploy,run}

say "Copying application"
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [[ "$SRC" != "$APP_DIR" ]]; then
  rsync -a --delete \
        --exclude '.venv' --exclude '__pycache__' --exclude '.git' \
        --exclude 'data/' --exclude 'output/' --exclude '.env' \
        "$SRC"/ "$APP_DIR"/
  echo "  synced from $SRC"
else
  echo "  already in place"
fi

say "Python environment"
if [[ ! -x "$APP_DIR/.venv/bin/python" ]]; then
  python3 -m venv "$APP_DIR/.venv"
fi
# piwheels serves aarch64 wheels; without it pandas/scipy compile from source.
"$APP_DIR/.venv/bin/pip" install -q --upgrade pip
"$APP_DIR/.venv/bin/pip" install -q \
    --extra-index-url https://www.piwheels.org/simple \
    pandas numpy
# scipy is optional — every test in this project has a numpy fallback. It is a
# large build if no wheel exists, so a failure here is not fatal.
if ! "$APP_DIR/.venv/bin/pip" install -q \
        --extra-index-url https://www.piwheels.org/simple scipy 2>/dev/null; then
  warn "scipy not installed — the code falls back to numpy implementations."
fi
"$APP_DIR/.venv/bin/python" -c "import pandas,numpy;print('  pandas',pandas.__version__,'numpy',numpy.__version__)"

say "API key"
if [[ ! -f "$APP_DIR/.env" ]]; then
  cat > "$APP_DIR/.env" <<'ENVEOF'
# Required. The dashboard refuses to run without it (REQUIRE_PROVIDER=polygon).
POLYGON_API_KEY=
# Optional: live ETF holdings for more accurate breadth.
FMP_API_KEY=
ENVEOF
  warn "created $APP_DIR/.env — PUT YOUR POLYGON KEY IN IT before the first run"
else
  echo "  .env already present (left untouched)"
fi
# The key must not be world-readable. 0600 and owned by the service account.
chmod 600 "$APP_DIR/.env"
chown "$APP_USER:$APP_USER" "$APP_DIR/.env"

say "Permissions"
chown -R "$APP_USER:$APP_USER" "$APP_DIR"
# Code read-only to the service; only data/ and output/ are writable, matching
# ReadWritePaths in the unit files.
find "$APP_DIR" -maxdepth 1 -name '*.py' -exec chmod 644 {} \;
chmod 750 "$APP_DIR/data" "$APP_DIR/output" "$APP_DIR/run"
chmod +x "$APP_DIR/deploy/refresh-runner.sh"
echo "  done"

say "systemd units"
install -m 644 "$APP_DIR/deploy/smf-dashboard.service" /etc/systemd/system/
install -m 644 "$APP_DIR/deploy/smf-refresh.service"   /etc/systemd/system/
install -m 644 "$APP_DIR/deploy/smf-refresh.timer"     /etc/systemd/system/
install -m 644 "$APP_DIR/deploy/smf-trigger.service"   /etc/systemd/system/
install -m 644 "$APP_DIR/deploy/smf-trigger.path"      /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now smf-dashboard.service
systemctl enable --now smf-refresh.timer
# The .path unit is what makes the in-page refresh buttons work.
systemctl enable --now smf-trigger.path
echo "  smf-dashboard.service enabled and started"
echo "  smf-refresh.timer enabled (weeknights 22:15 New York)"
echo "  smf-trigger.path enabled (in-page refresh buttons)"

say "Health check"
sleep 2
CODE=$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:$PORT/healthz" || echo 000)
if [[ "$CODE" == "200" ]]; then
  echo "  server responding on 127.0.0.1:$PORT"
else
  warn "server not responding (HTTP $CODE) — journalctl -u smf-dashboard -n 40"
fi

DASH=$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:$PORT/" || echo 000)
if [[ "$DASH" == "503" ]]; then
  warn "dashboard not generated yet — that is expected on a fresh install"
fi

cat <<NEXT

$(printf '\033[1m')Next steps$(printf '\033[0m')

  1. Add your Polygon key:
       sudo nano $APP_DIR/.env

  2. First data pull (COLD CACHE — expect 15-40 min on a Pi, ~480 series):
       sudo -u $APP_USER $APP_DIR/.venv/bin/python $APP_DIR/warm_cache.py
       sudo -u $APP_USER $APP_DIR/.venv/bin/python $APP_DIR/run.py

  3. Confirm locally:
       curl -sI http://127.0.0.1:$PORT/ | head -1

  4. Cloudflare Tunnel + Access — see deploy/README.md step 4.
     Nothing is reachable from the internet until you complete it.

NEXT
