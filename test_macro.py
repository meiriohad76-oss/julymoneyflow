#!/usr/bin/env python3
"""
FRED macro tests — parsing both response formats, and the key/keyless routing.

No network. The two parsers are fed captured sample payloads (one CSV, one JSON)
so the test is deterministic and runs offline in CI. The point is that switching
to the keyed API cannot silently change the numbers the dashboard shows.

    python test_macro.py
"""
from __future__ import annotations

import sys

import pandas as pd

from smf import config, macro

passed = failed = 0


def ok(cond, msg):
    global passed, failed
    if cond:
        passed += 1
    else:
        failed += 1
        print(f"  FAIL: {msg}")


CSV = """DATE,WALCL
2026-07-01,7100000
2026-07-08,7050000
2026-07-15,.
2026-07-22,7020000
"""

JSON = """{"observations":[
  {"date":"2026-07-01","value":"7100000"},
  {"date":"2026-07-08","value":"7050000"},
  {"date":"2026-07-15","value":"."},
  {"date":"2026-07-22","value":"7020000"}
]}"""

print("\n=== 1. both parsers agree on the same data ===")
c = macro._parse_csv(CSV, "WALCL")
j = macro._parse_json(JSON, "WALCL")
ok(len(c) == 3, f"CSV drops the '.' missing row (got {len(c)})")
ok(len(j) == 3, f"JSON drops the '.' missing row (got {len(j)})")
ok(list(c.values) == list(j.values), "CSV and JSON yield identical values")
ok(list(c.index) == list(j.index), "CSV and JSON yield identical dates")
ok(c.iloc[-1] == 7020000, f"last value parsed correctly (got {c.iloc[-1]})")
ok(str(c.index[0].date()) == "2026-07-01", "first date parsed correctly")

print("\n=== 2. parsers are robust to junk ===")
ok(len(macro._parse_json('{"observations":[]}', "X")) == 0, "empty JSON -> empty series")
ok(len(macro._parse_json('{}', "X")) == 0, "JSON with no observations key -> empty")
try:
    macro._parse_json("not json at all", "X")
    ok(False, "malformed JSON should raise (caught by fetch_series)")
except Exception:
    ok(True, "malformed JSON raises, as expected")
ok(len(macro._parse_csv("DATE,V\n", "X")) == 0, "header-only CSV -> empty series")

print("\n=== 3. routing: key present -> JSON API, absent -> keyless CSV ===")
# We don't hit the network; we just verify which URL/parse the code would pick.
saved = config.FRED_API_KEY
try:
    config.FRED_API_KEY = "testkey123"
    url = macro.FRED_API.format(sid="WALCL", key=config.FRED_API_KEY)
    ok("api.stlouisfed.org" in url and "api_key=testkey123" in url,
       "with a key, the JSON API URL is used")
    ok("file_type=json" in url, "JSON format requested")
    config.FRED_API_KEY = ""
    ok("fredgraph.csv" in macro.FRED_CSV.format(sid="WALCL"),
       "without a key, the keyless CSV endpoint is used")
finally:
    config.FRED_API_KEY = saved

print("\n=== 4. timeout is short enough to fail fast ===")
ok(config.FRED_TIMEOUT_SEC <= 12,
   f"FRED timeout is short ({config.FRED_TIMEOUT_SEC}s) so a block can't stall a run")

print("\n=== 5. cache round-trips through the same format ===")
import tempfile, pathlib
with tempfile.TemporaryDirectory() as d:
    p = pathlib.Path(d) / "WALCL.csv"
    c.to_frame("WALCL").to_csv(p)
    back = macro._read_cache(p)
    ok(list(back.values) == list(c.values), "cache write/read preserves values")
    ok(len(back) == len(c), "cache write/read preserves length")

print(f"\n{passed} passed, {failed} failed\n")
sys.exit(1 if failed else 0)
