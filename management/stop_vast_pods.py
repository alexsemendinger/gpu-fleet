#!/usr/bin/env python3
"""Stop Vast.ai instances (stops GPU billing; the instance keeps no data).

Parity with stop_pods.py for RunPod: targets currently-running instances,
prompts once, then calls client.stop_instance() on each. Treat a stop as a
destroy where data is concerned -- save anything that matters off the pod
first. Recreate with create_vast_pods.py.
"""
import argparse
import sys
import time

from mydotenv import load_env
load_env()

from vast_provider import get_vast_client, get_vast_pods


def stop_vast_pods(include_list, exclude_list):
    client = get_vast_client()
    pods = get_vast_pods(client)
    if not pods:
        print("No Vast.ai instances found.")
        return

    running = [p for p in pods if (p.get("status") or "").lower() == "running"]
    if not running:
        print("No running Vast.ai instances found.")
        return

    targets = []
    for pod in running:
        name = pod["name"]
        if name in exclude_list:
            continue
        if include_list and name not in include_list:
            continue
        targets.append(pod)

    if not targets:
        print("No Vast.ai instances match the specified criteria.")
        return

    print(f"\nFound {len(targets)} running Vast.ai instances to STOP:")
    for pod in targets:
        print(f"- {pod['name']} (id: {pod['id']})")

    print("\nNote: stopping preserves the disk; you still pay Vast storage fees.")
    if input(f"Stop these {len(targets)} instances? (y/N): ").lower() != "y":
        print("Operation cancelled.")
        return

    stopped, errors = 0, 0
    for pod in targets:
        try:
            print(f"Stopping {pod['name']} (id: {pod['id']})...", end=" ")
            client.stop_instance(id=pod["id"])
            print("✓")
            stopped += 1
            time.sleep(1)
        except Exception as e:
            print(f"\nError stopping {pod['name']}: {e}")
            errors += 1

    print(f"\nStopped: {stopped}  Errors: {errors}")
    print("Note: state transition may take a few moments on Vast's end.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Stop Vast.ai instances by label")
    parser.add_argument("--include", nargs="+", default=[],
                        help="Only stop these pod names (with or without the name prefix)")
    parser.add_argument("--exclude", nargs="+", default=[],
                        help="Stop all except these pod names (with or without the name prefix)")
    args = parser.parse_args()
    # Vast labels are full ("gpu-alder") names; accept bare names too.
    from pod_names import to_full
    stop_vast_pods([to_full(n) for n in args.include],
                   [to_full(n) for n in args.exclude])
