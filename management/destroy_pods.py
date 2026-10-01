#!/usr/bin/env python3
"""Destroy pods on both providers, running or stopped, in one step.

Usage:
  destroy_pods alder cedar             # these pods (bare or prefixed names)
  destroy_pods --all                   # every <prefix>-* pod on the account
  destroy_pods --all --exclude oak     # ...except these
  destroy_pods --all --yes             # no confirmation prompt (scripts, `at` jobs)

Destroying deletes the pod and everything on its disk. Network volumes are
separate objects and survive; delete those by hand (see NETWORK_VOLUMES.md).
Pods whose names don't start with MACHINE_NAME_PREFIX are never touched, even
with --all.
"""
import argparse
import os
import sys
import time

from mydotenv import load_env
load_env()
import runpod_compat as runpod
from pod_names import to_full

runpod.api_key = os.getenv("RUNPOD_API_KEY")
PREFIX = os.environ["MACHINE_NAME_PREFIX"] + "-"


def fleet():
    """[(provider, name, id, status, gpu)] for every pod on both providers."""
    pods = []
    for p in runpod.get_pods():
        pods.append(("runpod", p["name"], p["id"], p["_v2"].get("status", "?"),
                     (p.get("machine") or {}).get("gpuDisplayName") or "?"))
    if os.getenv("VASTAI_API_KEY"):
        from vast_provider import get_vast_pods
        for p in get_vast_pods():
            pods.append(("vast", p["name"] or "", p["id"], p.get("status", "?"), p.get("gpu", "?")))
    return pods


def main():
    ap = argparse.ArgumentParser(description="Destroy pods on both providers")
    ap.add_argument("names", nargs="*", help="pods to destroy (bare or prefixed)")
    ap.add_argument("--all", action="store_true", help=f"every {PREFIX}* pod")
    ap.add_argument("--exclude", nargs="+", default=[], help="never destroy these")
    ap.add_argument("--yes", "-y", action="store_true", help="skip the confirmation prompt")
    args = ap.parse_args()
    if bool(args.names) == args.all:
        ap.error("name the pods to destroy, or pass --all (not both)")

    exclude = {to_full(n) for n in args.exclude}
    pods = fleet()
    if args.all:
        targets = [p for p in pods if p[1].startswith(PREFIX) and p[1] not in exclude]
        others = sorted({p[1] for p in pods if not p[1].startswith(PREFIX)})
        if others:
            print(f"Leaving alone (not {PREFIX}*): {', '.join(others)}")
    else:
        wanted = {to_full(n) for n in args.names} - exclude
        targets = [p for p in pods if p[1] in wanted]
        missing = sorted(wanted - {p[1] for p in targets})
        if missing:
            print(f"Not found on either provider: {', '.join(missing)}")

    if not targets:
        print("Nothing to destroy.")
        return
    print(f"\nWill DESTROY {len(targets)} pod(s) — everything on their disks is lost:")
    for prov, name, _, status, gpu in targets:
        print(f"  {name:24s} {prov:7s} {status:13s} {gpu}")
    if not args.yes:
        if input("\nDestroy these pods? (y/N): ").strip().lower() != "y":
            print("Cancelled.")
            return

    failures = 0
    vast = None
    for prov, name, pid, _, _ in targets:
        try:
            if prov == "runpod":
                runpod.terminate_pod(pid)
            else:
                if vast is None:
                    from vast_provider import get_vast_client
                    vast = get_vast_client()
                vast.destroy_instance(id=pid)
            print(f"  destroyed {name}")
        except Exception as e:  # noqa: BLE001 - report and carry on with the rest
            print(f"  FAILED {name}: {e}")
            failures += 1

    # Confirm against the provider rather than trusting the calls.
    time.sleep(5)
    left = {p[1] for p in fleet()} & {t[1] for t in targets}
    if left:
        print(f"Still present after destroy (re-run to retry): {', '.join(sorted(left))}")
        failures += 1
    print(f"Done: {len(targets) - len(left)}/{len(targets)} destroyed.")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
