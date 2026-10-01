#!/usr/bin/env python3
"""Create RunPod pods, retrying until each one lands.

RunPod capacity is often tight: a single create attempt routinely comes back
"There are no longer any instances available...". So this runs one worker per
pod name, each retrying continuously and rotating through every GPU type under
the price cap until it gets a pod. A failed create creates nothing and costs
nothing, so it is safe to retry hard.

  * "no longer any instances available" and "This machine does not have the
    resources to deploy your pod" both mean "try again / try another type".
  * On-demand prices are fixed per GPU type; they are read live and only types
    at or under --max-price (per GPU) are tried, so the cap is a hard guarantee.
  * Names already up on EITHER provider are skipped, so re-running is safe.

USAGE
-----
  create_pods -n 12                    # make sure the first 12 names all have pods
  create_pods -a 4                     # 4 more pods, on the next unused names
  create_pods alder cedar              # these names
  create_pods -n 12 --max-price 0.40 --timeout 1800
  create_pods oak --gpu-types "NVIDIA A100-SXM4-80GB" --gpu-count 2 \\
      --cloud SECURE --max-price 2.00 --disk 500

Defaults come from config.env (RUNPOD_*). Afterwards run `ready_pods`, which
waits for IPs, updates the proxy and checks every pod.
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


def affordable_gpu_types(max_price, cloud, preferred):
    """GPU types whose per-GPU on-demand price in this cloud is <= max_price,
    cheapest first, with the preferred type (RUNPOD_GPU_TYPE) moved to the
    front so uniform fleets stay the default when capacity allows."""
    price_key = "securePrice" if cloud == "SECURE" else "communityPrice"
    keep = []
    for g in runpod.get_gpus():
        gid = g["id"]
        # CUDA images only: NVIDIA cards, and skip MIG slices (fractional GPUs
        # that frequently mis-provision for a full container workload).
        if not gid.startswith("NVIDIA") or "MIG" in gid:
            continue
        try:
            d = runpod.get_gpu(gid)
        except Exception:
            continue
        price = d.get(price_key)
        if price is not None and price <= max_price:
            keep.append((price, gid))
    keep.sort()
    types = [t for _, t in keep]
    if preferred in types:
        types.remove(preferred)
        types.insert(0, preferred)
    return types


def main():
    ap = argparse.ArgumentParser(description="Create RunPod pods, retrying until each lands")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("-n", "--num-machines", type=int,
                   help="make sure the first N names in MACHINE_NAME_LIST have pods")
    g.add_argument("-a", "--add", type=int, help="create N more pods on unused names")
    g.add_argument("names", nargs="*", default=[])
    ap.add_argument("--max-price", type=float, default=0.50,
                    help="hard cap on $/hr per GPU (default 0.50)")
    ap.add_argument("--gpu-types", "--gpu-type", dest="gpu_types",
                    help="comma-separated GPU types to try instead of every type under the cap")
    ap.add_argument("--gpu-count", type=int, default=int(os.getenv("RUNPOD_NUM_GPUS") or 1))
    ap.add_argument("--cloud", "--cloud-type", dest="cloud", choices=["COMMUNITY", "SECURE"],
                    default=os.getenv("RUNPOD_CLOUD_TYPE") or "COMMUNITY")
    ap.add_argument("--image", "--docker-image", dest="image",
                    default=os.environ["RUNPOD_DOCKER_IMAGE"])
    ap.add_argument("--disk", "--disk-space-in-gb", dest="disk", type=int,
                    default=int(os.environ["RUNPOD_DISK_SPACE_IN_GB"]),
                    help="container disk, GB")
    ap.add_argument("--volume", "--volume-space-in-gb", dest="volume", type=int,
                    default=int(os.environ["RUNPOD_VOLUME_SPACE_IN_GB"]),
                    help="pod volume at /workspace, GB (0 = none)")
    ap.add_argument("--timeout", type=int, default=1800, help="seconds before giving up")
    args = ap.parse_args()

    existing = used_pod_names()
    used_short = {n[len(PREFIX + "-"):] for n in existing if n.startswith(PREFIX + "-")}

    if args.names:
        # Accept names with or without the "<prefix>-" prefix.
        targets = [to_bare(n) for n in args.names]
    elif args.add:
        targets = [n for n in ALLOWED if n not in used_short][:args.add]
    elif args.num_machines:
        # The first N names, minus any that already have a pod: N in total.
        targets = ALLOWED[:args.num_machines]
    else:
        # No selection: refuse rather than default to every name. This creates
        # pods aggressively and without a prompt, so an accidental bare
        # invocation must be a no-op, not a fleet-sized spend.
        ap.error("select pods with -n N, -a N, or explicit names")

    targets = [n for n in targets if n not in used_short]
    if not targets:
        print("Nothing to create — all requested names already have pods.")
        return

    if args.gpu_types:
        gpu_types = [t.strip() for t in args.gpu_types.split(",")]
    else:
        preferred = os.getenv("RUNPOD_GPU_TYPE") or "NVIDIA RTX A4000"
        gpu_types = affordable_gpu_types(args.max_price, args.cloud, preferred)
    if not gpu_types:
        print(f"No {args.cloud} GPU types at or below ${args.max_price}/hr. Raise --max-price.")
        sys.exit(1)

    pubkey = read_pubkey()
    print(f"Targets ({len(targets)}): {targets}")
    print(f"{args.gpu_count}x GPU, {args.cloud}, types to try (cheapest first, "
          f"preferred type first): {gpu_types}")

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
            # bias the first few attempts toward the preferred GPU,
            # then rotate through every affordable type.
            gpu = gpu_types[0] if attempt <= 3 else gpu_types[(attempt - 1) % len(gpu_types)]
            # ONLY the create call may live in this try. If anything after a
            # successful create raises (e.g. BrokenPipeError from a closed
            # stdout, unexpected response shape), retrying would create a
            # DUPLICATE pod.
            try:
                res = runpod.create_pod(
                    name=full, image_name=args.image, gpu_count=args.gpu_count,
                    volume_in_gb=args.volume, container_disk_in_gb=args.disk,
                    ports="8888/http,22/tcp", volume_mount_path="/workspace",
                    gpu_type_id=gpu, cloud_type=args.cloud,
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
    print("\nNext: `ready_pods` — waits for IPs, updates the proxy, checks every pod.")


if __name__ == "__main__":
    main()
