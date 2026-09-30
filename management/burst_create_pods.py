#!/usr/bin/env python3
"""Burst-retry RunPod pod creator for when availability is tight.

WHY THIS EXISTS
---------------
`create_new_pods.py` (the `create_pods` wrapper) tries each pod name exactly
once. When RunPod community capacity is scarce — which is common — every
attempt comes back "There are no longer any instances available..." and you
get 0 pods with no retry. The morning provisioning one-liner then silently
does nothing.

This script does what a human operator does by hand in that situation, but
fast and in parallel: one worker thread per target name, each retrying
*continuously* and *rotating through every GPU type under the price cap*
until it lands a pod. Failed `create_pod` calls create nothing and cost
nothing, so it is safe to hammer.

KEY FACTS (learned the hard way; see CLAUDE.md "Provisioning under tight
availability"):
  * A failed create is free and leaves no pod — retry as fast as you like.
  * "no longer any instances available" and "This machine does not have the
    resources to deploy your pod" both mean "try again / try another type".
  * Community on-demand prices are fixed per GPU type; we query them live and
    only ever try types whose communityPrice <= --max-price, so the price cap
    is a hard guarantee.
  * Names already running on EITHER provider are skipped (no duplicates).

USAGE
-----
  burst_create_pods -n 12                 # ensure 12 pods (first 12 names), skip existing
  burst_create_pods -a 4                  # add 4 more using unused names
  burst_create_pods alder cedar ...      # specific bare names
  burst_create_pods -n 12 --max-price 0.40 --timeout 1800

Pairs with the standard flow: run this, wait until `list_pods` shows an IP
for every pod (never chain with &&; IPs take minutes to settle), then
`update_proxy` (plus `deploy_keys` on API-key days).
"""
import argparse
import ast
import os
import random
import sys
import threading
import time

from mydotenv import load_env
load_env()
import runpod_compat as runpod
from pod_boot import runpod_start_args

from pod_names import to_bare

runpod.api_key = os.getenv("RUNPOD_API_KEY")

PREFIX = os.environ["MACHINE_NAME_PREFIX"]
IMAGE = os.environ["RUNPOD_DOCKER_IMAGE"]
DISK = int(os.environ["RUNPOD_DISK_SPACE_IN_GB"])
VOL = int(os.environ["RUNPOD_VOLUME_SPACE_IN_GB"])
ALLOWED = ast.literal_eval(os.environ["MACHINE_NAME_LIST"])

# Optional boot hook (POD_SETUP_CMD in config.env); None boots the image as-is.
DOCKER_ARGS = runpod_start_args()

AVAIL_ERRORS = ("no longer any instances", "no instances available",
                "does not have the resources", "insufficient capacity",
                "no capacity", "unavailable")


def read_pubkey():
    p = os.getenv("SHARED_SSH_KEY_PATH")
    if not p:
        return ""
    pub = os.path.expanduser(p + ".pub")
    try:
        return open(pub).read().strip()
    except FileNotFoundError:
        print(f"WARNING: SSH public key not found at {pub}")
        return ""


def used_pod_names():
    """Full pod names in use across BOTH providers (skip to avoid dupes)."""
    used = set()
    try:
        for pod in runpod.get_pods() or []:
            if pod.get("name"):
                used.add(pod["name"])
    except Exception as e:
        print(f"# warn: runpod list failed: {e}")
    try:
        from vast_provider import get_vast_pods
        for pod in get_vast_pods():
            if pod.get("name"):
                used.add(pod["name"])
    except Exception as e:
        print(f"# warn: vast list failed: {e}")
    return used


def affordable_gpu_types(max_price):
    """Live-query RunPod GPU types and return those whose community on-demand
    price is <= max_price, cheapest first. Standard A4000 floated to the
    front so uniform fleets stay the default when capacity allows."""
    keep = []
    for g in runpod.get_gpus():
        gid = g["id"]
        # CUDA image only: NVIDIA cards, and skip MIG slices (fractional GPUs
        # that frequently mis-provision for a full container workload).
        if not gid.startswith("NVIDIA") or "MIG" in gid:
            continue
        try:
            d = runpod.get_gpu(gid)
        except Exception:
            continue
        price = d.get("communityPrice")
        if price is not None and price <= max_price:
            keep.append((price, gid))
    keep.sort()
    types = [t for _, t in keep]
    std = "NVIDIA RTX A4000"
    if std in types:
        types.remove(std)
        types.insert(0, std)
    return types


def main():
    ap = argparse.ArgumentParser(description="Burst-retry RunPod creator")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("-n", "--num-machines", type=int)
    g.add_argument("-a", "--add", type=int)
    g.add_argument("names", nargs="*", default=[])
    ap.add_argument("--max-price", type=float, default=0.50,
                    help="hard cap on community $/hr (default 0.50)")
    ap.add_argument("--gpu-types", help="comma-separated override of GPU types to try")
    ap.add_argument("--timeout", type=int, default=1800, help="seconds before giving up")
    args = ap.parse_args()

    existing = used_pod_names()
    used_short = {n[len(PREFIX + "-"):] for n in existing if n.startswith(PREFIX + "-")}

    if args.names:
        # Accept names with or without the "<prefix>-" prefix; everything below
        # works in bare names and re-adds the prefix per pod.
        targets = [to_bare(n) for n in args.names]
    elif args.add:
        unused = [n for n in ALLOWED if n not in used_short]
        targets = unused[:args.add]
    elif args.num_machines:
        # "ensure N total": take first N names not already up.
        targets = [n for n in ALLOWED if n not in used_short][:args.num_machines]
    else:
        # No selection given. Refuse rather than silently targeting every
        # missing name — this script creates pods aggressively and with no
        # confirmation prompt, so an accidental bare invocation must be a
        # no-op, not a fleet-sized spend.
        ap.error("select pods with -n N, -a N, or explicit names "
                 "(refusing to default to all missing names)")

    targets = [n for n in targets if n not in used_short]
    if not targets:
        print("Nothing to create — all requested names already running.")
        return

    if args.gpu_types:
        gpu_types = [t.strip() for t in args.gpu_types.split(",")]
    else:
        gpu_types = affordable_gpu_types(args.max_price)
    if not gpu_types:
        print(f"No GPU types at or below ${args.max_price}/hr. Raise --max-price.")
        sys.exit(1)

    pubkey = read_pubkey()
    print(f"Targets ({len(targets)}): {targets}")
    print(f"GPU types under ${args.max_price}/hr (cheapest first, A4000 prioritized):")
    print(f"  {gpu_types}")

    deadline = time.time() + args.timeout
    lock = threading.Lock()
    results = {}

    def worker(bare):
        full = f"{PREFIX}-{bare}"
        env = {"MACHINE_NAME": bare}
        if pubkey:
            env["PUBLIC_KEY"] = pubkey
        attempt = 0
        while time.time() < deadline:
            attempt += 1
            # bias the first few attempts toward the cheap standard GPU,
            # then rotate through every affordable type.
            gpu = gpu_types[0] if attempt <= 3 else gpu_types[(attempt - 1) % len(gpu_types)]
            # ONLY the create call may live in this try. If anything after a
            # successful create raises (e.g. BrokenPipeError from a closed
            # stdout, unexpected response shape), retrying would create a
            # DUPLICATE pod.
            try:
                res = runpod.create_pod(
                    name=full, image_name=IMAGE, gpu_count=1,
                    volume_in_gb=VOL, container_disk_in_gb=DISK,
                    ports="8888/http,22/tcp", volume_mount_path="/workspace",
                    gpu_type_id=gpu, cloud_type="COMMUNITY",
                    docker_args=DOCKER_ARGS, env=env,
                )
            except Exception as e:
                msg = str(e)
                if any(k in msg.lower() for k in AVAIL_ERRORS):
                    time.sleep(random.uniform(0.4, 1.2))
                    continue
                if attempt % 25 == 0:
                    print(f"[..] {full} attempt {attempt}: {msg[:110]}", flush=True)
                time.sleep(random.uniform(0.8, 1.5))
                continue
            pod_id = res.get("id") if isinstance(res, dict) else None
            with lock:
                results[bare] = (gpu, pod_id)
            print(f"[OK] {full} <- {gpu} (attempt {attempt}) id={pod_id}", flush=True)
            return
        print(f"[TIMEOUT] {full} gave up after {attempt} attempts", flush=True)

    threads = [threading.Thread(target=worker, args=(n,)) for n in targets]
    for t in threads:
        t.start()
    while any(t.is_alive() for t in threads):
        time.sleep(15)
        with lock:
            print(f"--- progress: {len(results)}/{len(targets)} landed: {sorted(results)} ---", flush=True)
    for t in threads:
        t.join()

    print(f"\n=== DONE: {len(results)}/{len(targets)} pods created ===")
    for n, (gpu, pid) in sorted(results.items()):
        print(f"  {PREFIX}-{n}: {gpu}  {pid}")
    print("\nNext: wait ~2-5 min for IPs, then `update_proxy` "
          "(plus `deploy_keys` on API-key days). "
          "Run `list_pods` to confirm every row has an IP.")


if __name__ == "__main__":
    main()
