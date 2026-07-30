# Raspberry Pi + Cloudflare Tunnel Deployment

Runs the dashboard on a Pi, refreshes it nightly after the US close, and reaches it
from anywhere behind Cloudflare Access email login.

**Nothing is exposed to the internet until step 4.** The Pi listens on loopback
only — no port forwarding, no open firewall port, and your home IP address is never
published.

---

## What gets deployed

| Piece | Role |
|---|---|
| `serve.py` | Read-only HTTP server, loopback only, serves an **allowlist** of two paths |
| `smf-dashboard.service` | Runs the server; heavily sandboxed |
| `smf-refresh.service` | Runs `run.py` to regenerate the dashboard |
| `smf-refresh.timer` | Weeknights 22:15 New York (45 min after the close) |
| `cloudflared-config.yml` | Tunnel pointing at `127.0.0.1:8080` |

---

## 1. Prepare the Pi

**Use 64-bit Raspberry Pi OS (Bookworm) or Ubuntu arm64.** On 32-bit there are no
prebuilt numpy/pandas wheels and you will compile for over an hour.

Pi 4 (4 GB) or Pi 5 recommended. A Pi Zero 2 W will work but a refresh takes
roughly 10 minutes and 512 MB is tight against the 1.2 GB memory ceiling.

```bash
sudo apt update && sudo apt full-upgrade -y
sudo raspi-config          # set the timezone; the timer uses America/New_York explicitly
```

## 2 & 3. Copy and install — one command

From Windows PowerShell in the project folder. Uses only `ssh`, `scp` and `tar`,
all of which ship with Windows 10 1803+:

```powershell
.\deploy\push-to-pi.ps1 -Pi pi@raspberrypi.local
```

This copies the code **and the price cache** (~19 MB compressed), then runs the
installer. Seeding the cache matters twice over: it saves the Pi a 15–40 minute
cold fetch, and it saves the Polygon API quota that fetch would spend. Add
`-NoCache` to skip it.

Then, on the Pi:

```bash
ssh pi@raspberrypi.local 'sudo nano /opt/smf/.env'        # add POLYGON_API_KEY

# with the cache seeded this makes ZERO API calls
ssh pi@raspberrypi.local 'sudo -u smf /opt/smf/.venv/bin/python /opt/smf/run.py --offline'
ssh pi@raspberrypi.local 'curl -sI http://127.0.0.1:8080/ | head -1'   # expect 200 OK
```

<details>
<summary>Manual alternative (macOS/Linux, or if you prefer rsync)</summary>

```bash
rsync -av --exclude '.venv' --exclude '__pycache__' --exclude '.git' \
      --exclude 'output/' --exclude '.env' --exclude 'node_modules' \
      "D:/smart money flow/" pi@raspberrypi.local:/tmp/smf/
ssh pi@raspberrypi.local 'sudo bash /tmp/smf/deploy/install-pi.sh'
```

Without a seeded cache the first build is a cold pull of ~480 series — 15–40
minutes, and it spends API quota. It is resumable:

```bash
sudo -u smf /opt/smf/.venv/bin/python /opt/smf/warm_cache.py
sudo -u smf /opt/smf/.venv/bin/python /opt/smf/run.py
```
</details>

## 3b. If the Pi already runs cloudflared — read this first

Learned the hard way on a live Pi. **Check before you touch anything:**

```bash
ps -eo pid,cmd | grep '[c]loudflared'
sudo ls -la /etc/cloudflared/
```

If a tunnel is already running, three things follow:

**Do not write to `/etc/cloudflared/config.yml`.** It belongs to the existing
tunnel. Overwriting it does not break the running process — that keeps its
config in memory — but the next reboot starts the wrong tunnel and takes every
other hostname on that Pi offline. Back it up first, always.

**Do not create a second tunnel.** Add a public hostname to the tunnel that
already exists. One tunnel serves many hostnames, and that is the supported
pattern:

> Zero Trust -> Networks -> Tunnels -> *your tunnel* -> Public Hostnames -> Add
>
> Subdomain `smf` · Domain your domain · Path **empty** · Type **HTTP** ·
> URL `localhost:<SMF_PORT>`

**Find out whether the tunnel is locally or remotely managed**, because it
decides where its routing lives:

```bash
sudo journalctl -u cloudflared | grep -m1 'Updated to new configuration'
```

A hit means **remotely managed** — the ingress rules live in the Cloudflare
dashboard, `config.yml` only names the tunnel and its credentials, and adding a
hostname in the UI takes effect within seconds with no restart. No hit means
locally managed, and the ingress rules are in `config.yml`.

`cloudflared tunnel route dns` also creates a CNAME pointing at whichever tunnel
you named. If you later attach the hostname to a *different* tunnel, delete that
record first under DNS -> Records, or the two conflict and you get **Error
1033**.

## 3c. Port conflicts

`8080` is a popular default and is often taken. The unit reads `SMF_PORT`, so a
clash is one override rather than an edit to a file the installer replaces:

```bash
sudo ss -ltn                                  # what is already listening
sudo mkdir -p /etc/systemd/system/smf-dashboard.service.d
printf '[Service]\nEnvironment=SMF_PORT=18080\n' \
  | sudo tee /etc/systemd/system/smf-dashboard.service.d/port.conf
sudo systemctl daemon-reload && sudo systemctl restart smf-dashboard
curl -sI http://127.0.0.1:18080/ | head -1    # want 200 OK
```

Confirm the origin returns **200** before pointing a tunnel at it. A tunnel to a
dead origin produces a 502 that looks like a tunnel fault and is not one.

## 4. Cloudflare Tunnel + Access

You need a domain on Cloudflare (free plan is fine).

```bash
# arm64 build
curl -L -o /tmp/cloudflared.deb \
  https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-arm64.deb
sudo dpkg -i /tmp/cloudflared.deb

cloudflared tunnel login                    # opens a browser link; pick your domain
cloudflared tunnel create smf               # note the UUID it prints
sudo mkdir -p /etc/cloudflared
sudo cp ~/.cloudflared/<UUID>.json /etc/cloudflared/
sudo cp /opt/smf/deploy/cloudflared-config.yml /etc/cloudflared/config.yml
sudo nano /etc/cloudflared/config.yml       # set <TUNNEL-UUID> and your hostname

cloudflared tunnel route dns smf smf.example.com
sudo cloudflared service install
sudo systemctl enable --now cloudflared
```

### Lock it down — do this before browsing to the hostname

Without an Access policy the URL is **public**. In the Cloudflare dashboard:

**Zero Trust → Access → Applications → Add an application → Self-hosted**

- Application domain: `smf.example.com`
- Session duration: 24 hours (or 1 week if you prefer fewer logins)
- Policy: **Allow** · Include → **Emails** → your address

Free for up to 50 users. Anyone else hitting the URL gets a login prompt and a
one-time code they cannot receive.

Verify from a phone on mobile data — not home wifi, which may be cached:

```
https://smf.example.com     -> Cloudflare login -> emailed code -> dashboard
```

Also worth adding under **Zero Trust → Settings → WAF**: rate limiting, and a
country block if you never travel.

---

## Refreshing from the browser

The dashboard header carries two buttons, which exist separately because they
cost different things:

| Button | What it does | Cost |
|---|---|---|
| **Rebuild from cache** | Recomputes every metric from data already on disk | Free |
| **Fetch latest data** | Pulls new prices from Polygon, then recomputes | **Spends API quota** |

"Fetch" asks for confirmation and is rate limited server-side to once every 15
minutes. "Rebuild" is unrestricted.

**The web server never runs the pipeline.** It is sandboxed with no network
access and no write access to `data/` or `output/`, and that is worth keeping. So
a button press writes one flag file into `/opt/smf/run/`, a systemd `.path` unit
notices it, and `smf-trigger.service` — which has exactly the permissions the
pipeline needs — does the work. The server's only new privilege is creating one
filename in one directory.

The buttons stay hidden unless the server reports the feature is available, so
`dashboard.html` opened straight from disk shows nothing that cannot work.

```bash
# check the plumbing
systemctl status smf-trigger.path
sudo journalctl -u smf-refresh -f          # watch a refresh run

# trigger one by hand, without the browser
echo offline | sudo -u smf tee /opt/smf/run/refresh.request
```

### Re-rendering after a code change

A presentation-only change does not need a recompute:

```bash
sudo -u smf /opt/smf/.venv/bin/python /opt/smf/run.py --render-only
```

Rebuilds `dashboard.html` from the last `snapshot.json` in **under a second**,
against roughly 40 seconds for `--offline` and minutes for a full fetch.

## Operating it

```bash
# status
systemctl status smf-dashboard cloudflared
systemctl list-timers smf-refresh

# force a refresh now
sudo systemctl start smf-refresh.service
journalctl -u smf-refresh -f

# logs
journalctl -u smf-dashboard -n 50
journalctl -u smf-refresh --since today
```

Change the schedule with `sudo systemctl edit smf-refresh.timer`.

---

## Security posture, and why each choice was made

**Allowlist, not a document root.** `output/` also contains your backtest CSVs,
`snapshot.json` and the research markdown. A normal static server rooted there
would publish all of it to anyone who guessed a filename. `serve.py` serves only
`/`, `/index.html` and `/dashboard.html`; **everything else 404s, including files
added later.** Verified:

```
/                          200
/dashboard.html            200
/healthz                   200
/snapshot.json             404
/backtest_observations.csv 404
/AUDIT_FINDINGS.md         404
/../.env                   404
/..%2f.env                 404
/output/../.env            404
POST / PUT / DELETE        501
```

**Loopback binding.** If the tunnel is down the dashboard is simply unreachable,
rather than quietly exposed on your LAN. The unit also sets `IPAddressDeny=any`
with `IPAddressAllow=localhost`, so the kernel enforces it even if the bind
argument is changed.

**The API key never travels.** `.env` is `0600`, owned by the `smf` service
account, and lives outside the served directory. Confirmed by scan: the key does
not appear in `dashboard.html`, `snapshot.json`, or any CSV. The dashboard is fully
self-contained — no CDN, no external fetches — so the CSP is `default-src 'none'`.

**systemd sandboxing.** The server runs with `ProtectSystem=strict`,
`ReadOnlyPaths=/opt/smf`, no capabilities, `MemoryDenyWriteExecute`, and a
syscall filter. It cannot write to disk at all. The refresh job gets network and
write access to `data/` and `output/` only, because it needs them.

**Non-login service account.** `smf` has `/usr/sbin/nologin` and no password.

---

## Performance on a Pi

| Task | Pi 4 (4GB) | Pi 5 | Notes |
|---|---|---|---|
| Cold cache warm | 15–40 min | 10–25 min | ~480 series, resumable |
| Nightly refresh | 1–3 min | under 1 min | warm cache |
| Serving a request | instant | instant | ~200 KB gzipped, cached in memory |

The dashboard is one self-contained HTML file — **741 KB raw, 172 KB gzipped**
(77% reduction), served from an mtime-keyed memory cache with ETag support, so
repeat loads return `304 Not Modified` and transfer nothing.

To rebuild the dashboard without any provider requests — useful when only the
rendering has changed, and the only way to be certain no API quota is spent:

```bash
sudo -u smf /opt/smf/.venv/bin/python /opt/smf/run.py --offline
```

---

## Troubleshooting

**`HTTP 503 — Dashboard has not been generated yet`** — `run.py` has not
succeeded. Check `journalctl -u smf-refresh -n 50`; the usual cause is a missing
or wrong `POLYGON_API_KEY`.

**Refresh fails with `ProviderUnavailable`** — the key is missing from
`/opt/smf/.env`, or the file is not readable by the `smf` user. Confirm with
`sudo -u smf cat /opt/smf/.env`.

**Tunnel connects but the browser 502s** — the server is not running. `systemctl
status smf-dashboard`. Check the port in `/etc/cloudflared/config.yml` matches
`--port` in the unit (8080 by default).

**Refresh is killed partway** — memory ceiling. Raise `MemoryMax` in
`smf-refresh.service`, or run `--tier1` only.

**Nothing at the hostname** — DNS route not created. `cloudflared tunnel route dns
smf smf.example.com`, then `cloudflared tunnel info smf`.

---

## Keeping it updated

```bash
rsync -av --exclude '.venv' --exclude '__pycache__' --exclude '.git' \
      --exclude 'data/' --exclude 'output/' --exclude '.env' \
      "D:/smart money flow/" pi@raspberrypi.local:/tmp/smf/
ssh pi@raspberrypi.local 'sudo bash /tmp/smf/deploy/install-pi.sh'
```

The installer is idempotent — it preserves `.env`, `data/` and `output/`, and
restarts the services.
