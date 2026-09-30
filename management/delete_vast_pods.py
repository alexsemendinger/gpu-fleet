#!/usr/bin/env python3
"""Destroy already-stopped Vast.ai instances (parity with delete_pods.py).

delete_pods.py for RunPod terminates pods in the EXITED state and leaves
RUNNING ones alone. This mirrors that for Vast: only instances whose
actual_status is in {stopped, exited} get destroyed. Use stop_vast_pods
first, or use kill_vast_pods.py to tear down running instances directly.
"""
import argparse
import sys
import time

from mydotenv import load_env
load_env()

from vast_provider import get_vast_client, get_vast_pods

STOPPED_STATES = {"stopped", "exited"}


def delete_stopped_vast_pods(include_list, exclude_list):
    client = get_vast_client()
    pods = get_vast_pods(client)
    if not pods:
        print("No Vast.ai instances found.")
        return

    stopped = [p for p in pods if (p.get("status") or "").lower() in STOPPED_STATES]
    if not stopped:
        print("No stopped Vast.ai instances found.")
        return

    targets = []
    for pod in stopped:
        name = pod["name"]
        if name in exclude_list:
            print(f"- {name} (id: {pod['id']}) - SKIPPING (in exclude list)")
            continue
        if include_list and name not in include_list:
            continue
        targets.append(pod)

    if not targets:
        print("No Vast.ai instances match the specified criteria.")
        return

    print(f"\nFound {len(targets)} stopped Vast.ai instances to DESTROY:")
    for pod in targets:
        print(f"- {pod['name']} (id: {pod['id']}, status: {pod['status']})")

    print("\nWARNING: destroying a Vast.ai instance permanently deletes its disk.")
    if input(f"Destroy these {len(targets)} stopped instances? (y/N): ").lower() != "y":
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
        except Exception as e:
            print(f"\nError destroying {pod['name']}: {e}")
            errors += 1

    print(f"\nDestroyed: {destroyed}  Errors: {errors}")
    print("Note: teardown may take a few moments on Vast's end.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Destroy stopped Vast.ai instances by label")
    parser.add_argument("--include", nargs="+", default=[],
                        help="Only destroy these pod names (with or without the name prefix)")
    parser.add_argument("--exclude", nargs="+", default=[],
                        help="Destroy all stopped except these pod names (with or without the name prefix)")
    args = parser.parse_args()
    # Vast labels are full ("gpu-alder") names; accept bare names too.
    from pod_names import to_full
    delete_stopped_vast_pods([to_full(n) for n in args.include],
                             [to_full(n) for n in args.exclude])
