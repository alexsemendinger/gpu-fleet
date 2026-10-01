#!/usr/bin/env python3
"""Mint per-pod OpenRouter API keys via the provisioning API.

Key names are `<prefix>-<podname>-<KEY_COHORT>` (e.g. gpu-alder-fall26).
Pod names come from MACHINE_NAME_LIST in config.env — the same list every
other script uses — so new pods added to the list get keys automatically
and retired names don't.

Set KEY_COHORT in config.env to a new value each program cycle;
openrouter_spend_report.py uses the same value to find (and disable)
this cycle's keys.

Usage:
  # provisioning key: keys/.openrouter_provisioning_key, or the env var
  python3 generate_openrouter_keys.py     # writes keys/openrouter_api_keys.csv
"""
import argparse
import ast
import csv
import json
import os
import sys
import urllib.error
import urllib.request

from mydotenv import load_env
load_env()

COHORT = os.environ.get("KEY_COHORT") or sys.exit("Set KEY_COHORT in config.env (e.g. fall26)")
LIMIT_USD = 10  # per-key spending cap

KEYS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "keys")
PROV_KEY_PATH = os.path.join(KEYS_DIR, ".openrouter_provisioning_key")

PROVISIONING_KEY = os.environ.get("OPENROUTER_PROVISIONING_KEY")
if not PROVISIONING_KEY and os.path.exists(PROV_KEY_PATH):
    PROVISIONING_KEY = open(PROV_KEY_PATH).read().strip()
if not PROVISIONING_KEY:
    print(f"No provisioning key: put it in {PROV_KEY_PATH} "
          "or set OPENROUTER_PROVISIONING_KEY")
    sys.exit(1)

PREFIX = os.environ.get("MACHINE_NAME_PREFIX", "gpu")
POD_NAMES = ast.literal_eval(os.environ["MACHINE_NAME_LIST"])

ap = argparse.ArgumentParser(description="Mint one capped OpenRouter key per pod name")
ap.add_argument("--limit", type=float, default=LIMIT_USD, help=f"$ cap per key (default {LIMIT_USD})")
ap.add_argument("--force", action="store_true",
                help="replace an existing keys/openrouter_api_keys.csv (its keys are NOT disabled)")
args = ap.parse_args()

os.makedirs(KEYS_DIR, exist_ok=True)
OUT_PATH = os.path.join(KEYS_DIR, "openrouter_api_keys.csv")
if os.path.exists(OUT_PATH) and not args.force:
    sys.exit(f"{OUT_PATH} already exists. Keep it, or re-run with --force to replace it "
             "(disable the old keys first with openrouter_spend_report.py --disable).")


def api(method, path, body=None):
    req = urllib.request.Request(
        f"https://openrouter.ai/api/v1{path}", method=method,
        headers={"Authorization": f"Bearer {PROVISIONING_KEY}", "Content-Type": "application/json"},
        data=json.dumps(body).encode() if body is not None else None)
    return json.loads(urllib.request.urlopen(req, timeout=30).read())


# Check the provisioning key once before minting anything.
try:
    api("GET", "/keys")
except urllib.error.HTTPError as e:
    sys.exit(f"OpenRouter rejected the provisioning key (HTTP {e.code}); nothing was minted.")

rows, failed = [], []
for name in POD_NAMES:
    try:
        resp = api("POST", "/keys", {"name": f"{PREFIX}-{name}-{COHORT}", "limit": args.limit})
        rows.append([f"{PREFIX}-{name}", resp.get("key") or resp["data"]["key"]])
        print(f"  {PREFIX}-{name}: created")
    except Exception as e:  # noqa: BLE001
        failed.append(name)
        print(f"  {PREFIX}-{name}: FAILED — {e}")

# Write whatever was minted, so no live key goes unrecorded.
if rows:
    tmp = OUT_PATH + ".tmp"
    with open(tmp, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["machine_name", "openrouter_api_key"])
        writer.writerows(rows)
    os.chmod(tmp, 0o600)
    os.replace(tmp, OUT_PATH)
    print(f"\nWrote {len(rows)} key(s) to {OUT_PATH}")
if failed:
    sys.exit(f"Failed for {len(failed)} name(s): {', '.join(failed)}")
