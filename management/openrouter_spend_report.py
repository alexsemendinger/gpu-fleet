#!/usr/bin/env python3
"""Record per-pod OpenRouter key spend, and optionally disable them.

Lists every key in the OpenRouter workspace via the provisioning API,
filters to the current cohort (see `--pattern`), writes a timestamped CSV
report under `keys/spend_reports/`, and (with `--disable`) PATCHes
each key to `disabled: true`.

Always run `--report-only` first to verify which keys will be touched.
Disabling is irreversible from this script's side — you'd need to mint
new keys with `generate_openrouter_keys.py` to restore service.

Usage:
  python3 openrouter_spend_report.py                      # report only
  python3 openrouter_spend_report.py --pattern cohort_name # different pattern suffix
  python3 openrouter_spend_report.py --disable             # report + disable
"""
import argparse
import csv
import fnmatch
import json
import os
import sys
import urllib.request
import urllib.error
from datetime import datetime, timezone

from mydotenv import load_env
load_env()

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
KEYS_DIR = os.path.join(BASE_DIR, "keys")
PROV_KEY_PATH = os.path.join(KEYS_DIR, ".openrouter_provisioning_key")
REPORTS_DIR = os.path.join(KEYS_DIR, "spend_reports")

API_ROOT = "https://openrouter.ai/api/v1"


def load_prov_key():
    with open(PROV_KEY_PATH, "r") as f:
        return f.read().strip()


def http(method, path, prov_key, body=None):
    url = f"{API_ROOT}{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        url,
        method=method,
        data=data,
        headers={
            "Authorization": f"Bearer {prov_key}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        return e.code, {"_error": body}


def list_keys(prov_key):
    """List every key in the workspace, including disabled ones."""
    status, payload = http(
        "GET", "/keys?include_disabled=true", prov_key,
    )
    if status != 200:
        sys.exit(f"List failed: HTTP {status}: {payload}")
    return payload.get("data", [])


def disable_key(prov_key, key_hash):
    status, payload = http(
        "PATCH", f"/keys/{key_hash}", prov_key, body={"disabled": True}
    )
    return status, payload


def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument(
        "--pattern",
        default=f"{os.environ.get('MACHINE_NAME_PREFIX', 'gpu')}-*-{os.environ.get('KEY_COHORT') or '*'}",
        help="Glob for key names to include (default: <prefix>-*-<KEY_COHORT> "
             "from config.env, i.e. this cycle's keys from generate_openrouter_keys.py).",
    )
    ap.add_argument(
        "--disable", action="store_true",
        help="After writing the report, PATCH each matched key to disabled=true.",
    )
    args = ap.parse_args()

    prov_key = load_prov_key()
    if not prov_key:
        sys.exit(f"Provisioning key file empty: {PROV_KEY_PATH}")

    print(f"Listing OpenRouter keys (include_disabled=true)...")
    all_keys = list_keys(prov_key)
    print(f"  {len(all_keys)} total keys in workspace.")

    matched = [k for k in all_keys if fnmatch.fnmatchcase(k.get("name", ""), args.pattern)]
    matched.sort(key=lambda k: k.get("name", ""))
    print(f"  {len(matched)} match pattern {args.pattern!r}.\n")

    if not matched:
        print("No matching keys. Exiting without writing report.")
        return 0

    os.makedirs(REPORTS_DIR, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")
    report_path = os.path.join(REPORTS_DIR, f"spend_report_{stamp}.csv")

    fields = [
        "name", "hash", "disabled", "usage_usd", "limit_usd",
        "limit_remaining_usd", "created_at", "updated_at",
    ]
    total_spend = 0.0
    with open(report_path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for k in matched:
            usage = float(k.get("usage") or 0)
            total_spend += usage
            w.writerow({
                "name": k.get("name"),
                "hash": k.get("hash"),
                "disabled": k.get("disabled"),
                "usage_usd": round(usage, 6),
                "limit_usd": k.get("limit"),
                "limit_remaining_usd": k.get("limit_remaining"),
                "created_at": k.get("created_at"),
                "updated_at": k.get("updated_at"),
            })

    print(f"Wrote report: {report_path}\n")
    # Console summary
    print(f"  {'Name':<28} {'Usage ($)':>10}  {'Limit ($)':>10}  Disabled")
    print(f"  {'-'*28} {'-'*10}  {'-'*10}  --------")
    for k in matched:
        print(f"  {(k.get('name') or '?'):<28} "
              f"{float(k.get('usage') or 0):>10.4f}  "
              f"{(k.get('limit') if k.get('limit') is not None else '-'):>10}  "
              f"{k.get('disabled')}")
    print(f"  {'-'*28} {'-'*10}")
    print(f"  {'TOTAL':<28} {total_spend:>10.4f}\n")

    if not args.disable:
        print("Report-only mode. Re-run with --disable to disable these keys.")
        return 0

    # Disable phase
    already_disabled = [k for k in matched if k.get("disabled")]
    if already_disabled:
        print(f"Note: {len(already_disabled)} key(s) already disabled; will be skipped.\n")
    to_disable = [k for k in matched if not k.get("disabled")]
    if not to_disable:
        print("Nothing to disable. Exiting.")
        return 0

    print(f"Disabling {len(to_disable)} key(s)...")
    failures = []
    for k in to_disable:
        status, payload = disable_key(prov_key, k["hash"])
        if 200 <= status < 300:
            print(f"  [OK  ] {k.get('name')}")
        else:
            print(f"  [FAIL] {k.get('name')}: HTTP {status}: {payload}")
            failures.append(k.get("name"))

    print()
    print(f"Disabled {len(to_disable) - len(failures)}/{len(to_disable)}.")
    if failures:
        print(f"Failures: {failures}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
