#!/usr/bin/env python3
"""Destroy Vast.ai instances by label (teardown parity with kill_pods.py).

Destroying an instance permanently deletes its disk. For a non-destructive
teardown that preserves the volume, use stop_vast_pods.py instead -- Vast
*does* expose stop/start via the SDK (`client.stop_instance` /
`client.start_instance`), despite what older comments in this repo claimed.
"""
import argparse
import os
import sys
import time

from mydotenv import load_env
load_env()

from vast_provider import get_vast_client, get_vast_pods


def kill_vast_pods(include_list, exclude_list):
    client = get_vast_client()
    pods = get_vast_pods(client)
    if not pods:
        print("No Vast.ai instances found.")
        return

    targets = []
    for pod in pods:
        name = pod["name"]
        if name in exclude_list:
            continue
        if include_list and name not in include_list:
            continue
        targets.append(pod)

    if not targets:
        print("No Vast.ai instances match the specified criteria.")
        return

    print(f"\nFound {len(targets)} Vast.ai instances to DESTROY:")
    for pod in targets:
        print(f"- {pod['name']} (id: {pod['id']}, status: {pod['status']})")

    print("\nWARNING: destroying a Vast.ai instance deletes ALL its data.")
    if input(f"Destroy these {len(targets)} instances? (y/N): ").lower() != "y":
        print("Operation cancelled.")
        return

    destroyed, errors = 0, 0
    for pod in targets:
        try:
            print(f"Destroying {pod['name']} (id: {pod['id']})...", end=" ")
            client.destroy_instance(id=pod["id"])
            print("✓")
            destroyed += 1
            time.sleep(1)
        except Exception as e:  # noqa: BLE001
            print(f"\nError destroying {pod['name']}: {e}")
            errors += 1

    print(f"\nDestroyed: {destroyed}  Errors: {errors}")
    print("Note: teardown may take a few moments on Vast's end.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Destroy Vast.ai instances by label")
    parser.add_argument("--include", nargs="+", default=[],
                        help="Only destroy these pod names (with or without the name prefix)")
    parser.add_argument("--exclude", nargs="+", default=[],
                        help="Destroy all except these pod names (with or without the name prefix)")
    args = parser.parse_args()
    # Vast labels are full ("gpu-alder") names; accept bare names too.
    from pod_names import to_full
    kill_vast_pods([to_full(n) for n in args.include],
                   [to_full(n) for n in args.exclude])
