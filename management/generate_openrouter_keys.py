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
import ast
import csv
import json
import os
import sys
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

os.makedirs(KEYS_DIR, exist_ok=True)
OUT_PATH = os.path.join(KEYS_DIR, "openrouter_api_keys.csv")

with open(OUT_PATH, "w", newline="") as f:
    writer = csv.writer(f)
    writer.writerow(["machine_name", "openrouter_api_key"])
    for name in POD_NAMES:
        req = urllib.request.Request(
            "https://openrouter.ai/api/v1/keys",
            method="POST",
            headers={
                "Authorization": f"Bearer {PROVISIONING_KEY}",
                "Content-Type": "application/json",
            },
            data=json.dumps({
                "name": f"{PREFIX}-{name}-{COHORT}",
                "limit": LIMIT_USD,
            }).encode(),
        )
        try:
            resp = json.loads(urllib.request.urlopen(req).read())
            key = resp.get("key") or resp["data"]["key"]
            print(f"  {PREFIX}-{name}: created")
            writer.writerow([f"{PREFIX}-{name}", key])
        except Exception as e:
            print(f"  {PREFIX}-{name}: FAILED — {e}")

print(f"\nWrote {OUT_PATH}")
