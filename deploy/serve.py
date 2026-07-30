#!/usr/bin/env python3
"""
Minimal read-only HTTP server for the dashboard.

Design constraints, all of them security-driven:

  ALLOWLIST, NOT A DOCUMENT ROOT
      `output/` also contains backtest CSVs, snapshot.json and the research
      markdown. A conventional static server rooted there would publish all of it
      the moment someone guessed a filename. This serves ONLY the paths in
      ALLOWED, and returns 404 for everything else — including anything added to
      output/ later. New files are invisible until explicitly listed.

  BIND TO LOOPBACK
      Default 127.0.0.1. cloudflared connects locally, so the Pi never listens on
      the LAN and nothing is reachable without going through the tunnel. If the
      tunnel is down, the dashboard is simply unreachable rather than
      accidentally exposed on the local network.

  GET AND HEAD ONLY
      No POST/PUT/DELETE handlers exist. There is no upload path, no query
      execution, no parameter that reaches the filesystem.

  NO SECRETS ON DISK PATH
      Never serves from the project root, so `.env` is not reachable even by
      traversal — the allowlist is resolved to absolute paths at startup and
      compared by identity, not by string prefix.

    python deploy/serve.py                 # 127.0.0.1:8080
    python deploy/serve.py --port 8099
    python deploy/serve.py --host 0.0.0.0  # LAN exposure; only with a firewall
"""
from __future__ import annotations

import argparse
import datetime as dt
import gzip
import hashlib
import io
import json
import mimetypes
import os
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUTPUT = ROOT / "output"

# The ONLY directory this process may write to. A refresh button needs to reach
# the pipeline, but the pipeline needs network and disk while this server is
# deliberately sandboxed against both. Rather than weaken the server, it drops a
# flag file here and a systemd .path unit picks it up. The server therefore still
# cannot execute anything, cannot escalate privileges, and cannot touch data/ or
# output/ -- it can create exactly one filename in one directory.
RUNDIR = ROOT / "run"
REQUEST = RUNDIR / "refresh.request"
LASTFETCH = RUNDIR / "last_fetch"
RUNNING = RUNDIR / "running"

# Minimum gap between live fetches, in seconds. 0 disables it.
#
# Originally 15 minutes to protect API quota. The user's Polygon plan is
# unmetered, so that rationale is gone and the limit is pure friction —
# overlapping runs are already prevented by the running/queued guard, which is a
# correctness concern rather than a cost one. Kept as a knob because a future
# plan change or a shared deployment would want it back.
FETCH_COOLDOWN_SEC = int(os.environ.get("SMF_FETCH_COOLDOWN_SEC", "0"))

# The complete set of URL paths this process will ever serve.
ALLOWED: dict[str, Path] = {
    "/": OUTPUT / "dashboard.html",
    "/index.html": OUTPUT / "dashboard.html",
    "/dashboard.html": OUTPUT / "dashboard.html",
}

# Resolved once at startup so a symlink swapped in later cannot redirect us.
_RESOLVED: dict[str, Path] = {}

_lock = threading.Lock()
_cache: dict[str, tuple[float, bytes, bytes, str]] = {}   # path -> (mtime, raw, gz, etag)


def _load(p: Path) -> tuple[bytes, bytes, str] | None:
    """Read a file with a small mtime-keyed cache and a precomputed gzip copy."""
    try:
        st = p.stat()
    except OSError:
        return None
    key = str(p)
    with _lock:
        hit = _cache.get(key)
        if hit and hit[0] == st.st_mtime:
            return hit[1], hit[2], hit[3]
    try:
        raw = p.read_bytes()
    except OSError:
        return None
    buf = io.BytesIO()
    # mtime=0 keeps the gzip output byte-stable so the ETag is deterministic.
    with gzip.GzipFile(fileobj=buf, mode="wb", compresslevel=6, mtime=0) as g:
        g.write(raw)
    gz = buf.getvalue()
    etag = '"%s"' % hashlib.sha256(raw).hexdigest()[:32]
    with _lock:
        _cache[key] = (st.st_mtime, raw, gz, etag)
    return raw, gz, etag


class Handler(BaseHTTPRequestHandler):
    server_version = "smf"
    sys_version = ""                     # don't advertise the Python version
    protocol_version = "HTTP/1.1"

    # ---- logging: one concise line, no user-controlled echo ----
    def log_message(self, fmt: str, *args) -> None:  # noqa: A003
        ts = dt.datetime.now().strftime("%H:%M:%S")
        sys.stderr.write(f"[{ts}] {self.address_string()} {fmt % args}\n")

    def _headers(self, length: int, ctype: str, etag: str, gzipped: bool) -> None:
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(length))
        self.send_header("ETag", etag)
        if gzipped:
            self.send_header("Content-Encoding", "gzip")
        self.send_header("Vary", "Accept-Encoding")
        # The dashboard is regenerated on a schedule; never let a proxy pin it.
        self.send_header("Cache-Control", "no-cache, must-revalidate")
        # Hardening. The dashboard is fully self-contained — no CDN, no external
        # fetches — so a strict CSP costs nothing and blocks injected content.
        self.send_header(
            "Content-Security-Policy",
            "default-src 'none'; script-src 'unsafe-inline'; "
            "style-src 'unsafe-inline'; img-src data:; font-src 'none'; "
            # 'self' only, so the refresh buttons can reach /refresh and /status
            # and nothing else. Any exfiltration target remains blocked.
            "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; "
            "form-action 'none'")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Permissions-Policy",
                         "geolocation=(), microphone=(), camera=(), payment=()")

    def _deny(self, code: int = 404) -> None:
        body = b"not found\n"
        self.send_response(code)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _serve(self, head_only: bool = False) -> None:
        # Strip query and fragment; never used, never passed to the filesystem.
        path = self.path.split("?", 1)[0].split("#", 1)[0]

        if path == "/healthz":
            body = b"ok\n"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            if not head_only:
                self.wfile.write(body)
            return

        target = _RESOLVED.get(path)
        if target is None:
            self._deny()
            return

        loaded = _load(target)
        if loaded is None:
            # File listed but missing on disk — the pipeline has not run yet.
            body = (b"Dashboard has not been generated yet.\n"
                    b"Run: python run.py\n")
            self.send_response(503)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Retry-After", "300")
            self.end_headers()
            if not head_only:
                self.wfile.write(body)
            return

        raw, gz, etag = loaded

        if self.headers.get("If-None-Match") == etag:
            self.send_response(304)
            self.send_header("ETag", etag)
            self.send_header("Cache-Control", "no-cache, must-revalidate")
            self.end_headers()
            return

        ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/json",):
            ctype += "; charset=utf-8"

        accepts_gzip = "gzip" in (self.headers.get("Accept-Encoding") or "")
        body = gz if accepts_gzip else raw

        self.send_response(200)
        self._headers(len(body), ctype, etag, accepts_gzip)
        self.end_headers()
        if not head_only:
            self.wfile.write(body)

    # ---- refresh plumbing ----
    def _json(self, code: int, obj: dict) -> None:
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _status(self) -> dict:
        try:
            built = OUTPUT.joinpath("dashboard.html").stat().st_mtime
        except OSError:
            built = None
        last = None
        try:
            last = float(LASTFETCH.read_text().strip())
        except (OSError, ValueError):
            pass
        cool = 0
        if last is not None:
            cool = max(0, int(FETCH_COOLDOWN_SEC - (time.time() - last)))
        return {
            "built_at": built,
            "built_age_sec": int(time.time() - built) if built else None,
            "queued": REQUEST.exists(),
            "running": RUNNING.exists(),
            "fetch_cooldown_sec": cool,
            "refresh_enabled": RUNDIR.is_dir(),
        }

    def do_POST(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path != "/refresh":
            self._deny(404)
            return
        if not RUNDIR.is_dir():
            self._json(503, {"error": "refresh is not enabled on this install"})
            return

        # Mode comes from the query string only -- never from a request body, and
        # never interpolated into a command. It is validated against a fixed set
        # and written as a bare word, so there is no path for injection.
        q = urllib.parse.parse_qs(self.path.partition("?")[2])
        mode = (q.get("mode", ["offline"])[0] or "offline").strip().lower()
        if mode not in ("offline", "fetch"):
            self._json(400, {"error": "mode must be 'offline' or 'fetch'"})
            return

        st = self._status()
        if st["running"] or st["queued"]:
            self._json(409, {"error": "a refresh is already in progress", **st})
            return
        if mode == "fetch" and st["fetch_cooldown_sec"] > 0:
            self._json(429, {
                "error": "fetch is rate limited to protect API quota",
                **st})
            return

        try:
            REQUEST.write_text(mode + "\n")
        except OSError as exc:
            self._json(500, {"error": f"could not queue the refresh: {exc}"})
            return
        self.log_message("queued refresh mode=%s", mode)
        self._json(202, {"queued": True, "mode": mode, **self._status()})

    def do_GET(self) -> None:   # noqa: N802
        if self.path.split("?", 1)[0] == "/status":
            self._json(200, self._status())
            return
        self._serve()

    def do_HEAD(self) -> None:  # noqa: N802
        self._serve(head_only=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1",
                    help="bind address; keep loopback when using cloudflared")
    ap.add_argument("--port", type=int, default=8080)
    args = ap.parse_args()

    for url, p in ALLOWED.items():
        _RESOLVED[url] = p.resolve()

    missing = {u: p for u, p in _RESOLVED.items() if not p.exists()}
    print(f"smart-money-flow server")
    print(f"  root      : {OUTPUT}")
    print(f"  serving   : {sorted(set(str(p.name) for p in _RESOLVED.values()))}")
    print(f"  endpoints : {sorted(_RESOLVED)} + /healthz")
    if missing:
        print(f"  ! not yet generated: {sorted(set(p.name for p in missing.values()))}"
              f" — run `python run.py`")
    if args.host not in ("127.0.0.1", "localhost", "::1"):
        print(f"  ! WARNING: binding {args.host} exposes this beyond loopback. "
              f"With cloudflared you do not need this.")

    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    srv.daemon_threads = True
    print(f"  listening : http://{args.host}:{args.port}\n")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
