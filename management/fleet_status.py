#!/usr/bin/env python3
"""Per-name provisioning status for every pod in MACHINE_NAME_LIST.

One line per name, machine-readable, so shell loops can decide what to do:

    alder     HAS_IP   1.2.3.4:40123  runpod  RTX A4000
    birch     NO_IP    -              runpod  RTX A5000
    cedar     MISSING  -              -       -

  MISSING — no instance on either provider; needs creating.
  NO_IP   — instance exists but has no public SSH endpoint yet: still
            booting/pulling the image, or it drew a host that never
            publishes one (see the NO-IP note in CLAUDE.md).
  HAS_IP  — reachable endpoint exists; the proxy can route to it.

`--missing` / `--no-ip` / `--has-ip` print just those bare names on one
space-separated line, ready to splice into a wrapper invocation.
"""
import argparse
import ast
import os
import sys

from mydotenv import load_env
load_env()

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from vast_provider import get_vast_pods  # noqa: E402

import runpod_compat as runpod  # noqa: E402


def runpod_endpoints():
    """{pod_name: (ip:port|None, gpu)} for every RunPod pod."""
    runpod.api_key = os.environ["RUNPOD_API_KEY"]
    out = {}
    for p in runpod.get_pods():
        endpoint = None
        rt = p.get("runtime") or {}
        for port in rt.get("ports") or []:
            if port.get("privatePort") == 22 and port.get("ip") and port.get("isIpPublic"):
                endpoint = f"{port['ip']}:{port['publicPort']}"
                break
        gpu = ((p.get("machine") or {}).get("gpuDisplayName")) or "?"
        out[p.get("name")] = (endpoint, gpu)
    return out


def vast_endpoints():
    out = {}
    for p in get_vast_pods():
        ip, port = p.get("ip"), p.get("ssh_port")
        out[p["name"]] = (f"{ip}:{port}" if ip and port else None, p.get("gpu") or "?")
    return out


def main():
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--missing", action="store_true")
    g.add_argument("--no-ip", action="store_true")
    g.add_argument("--has-ip", action="store_true")
    ap.add_argument("--count", action="store_true", help="print 'has_ip/total' only")
    args = ap.parse_args()

    prefix = os.environ["MACHINE_NAME_PREFIX"]
    names = ast.literal_eval(os.environ["MACHINE_NAME_LIST"])

    try:
        rp = runpod_endpoints()
    except Exception as e:  # noqa: BLE001
        sys.exit(f"fleet_status: could not list RunPod pods: {e}")
    try:
        vs = vast_endpoints()
    except Exception:  # Vast unreachable shouldn't blind us to RunPod
        vs = {}

    rows = []
    for name in names:
        full = f"{prefix}-{name}"
        if full in rp:
            endpoint, gpu, provider = (*rp[full], "runpod")
        elif full in vs:
            endpoint, gpu, provider = (*vs[full], "vast")
        else:
            endpoint, gpu, provider = None, "-", "-"
            rows.append((name, "MISSING", "-", provider, gpu))
            continue
        rows.append((name, "HAS_IP" if endpoint else "NO_IP",
                     endpoint or "-", provider, gpu))

    if args.count:
        print(f"{sum(1 for r in rows if r[1] == 'HAS_IP')}/{len(rows)}")
        return
    for flag, state in (("missing", "MISSING"), ("no_ip", "NO_IP"), ("has_ip", "HAS_IP")):
        if getattr(args, flag):
            print(" ".join(r[0] for r in rows if r[1] == state))
            return
    for name, state, endpoint, provider, gpu in rows:
        print(f"{name:<12} {state:<8} {endpoint:<22} {provider:<7} {gpu}")


if __name__ == "__main__":
    main()
