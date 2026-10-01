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



def read_pubkey():
    """The shared public key every pod must trust. Fatal if missing: a pod
    without it bills while nobody can log in."""
    p = os.getenv("SHARED_SSH_KEY_PATH")
    if not p:
        sys.exit("SHARED_SSH_KEY_PATH is not set in config.env.")
    pub = os.path.expanduser(os.path.expandvars(p)) + ".pub"
    try:
        key = open(pub).read().strip()
    except OSError as e:
        sys.exit(f"Can't read the shared SSH public key {pub}: {e}")
    if not key:
        sys.exit(f"The shared SSH public key {pub} is empty.")
    return key


def used_pod_names():
    """Full pod names in use across BOTH providers. A listing failure aborts:
    guessing "nothing exists" would create a duplicate of every pod."""
    used = set()
    try:
        for pod in runpod.get_pods() or []:
            if pod.get("name"):
                used.add(pod["name"])
    except Exception as e:  # noqa: BLE001
        sys.exit(f"Couldn't list existing RunPod pods, so not creating anything "
                 f"(it could duplicate pods): {e}")
    try:
        from vast_provider import get_vast_pods
        for pod in get_vast_pods(strict=True):
            if pod.get("name"):
                used.add(pod["name"])
    except Exception as e:  # noqa: BLE001
        sys.exit(f"Couldn't list existing Vast.ai pods, so not creating anything: {e}")
    return used


def gpu_prices(cloud):
    """{gpu type id: per-GPU on-demand $/hr in this cloud} for usable types:
    NVIDIA only (the images are CUDA), no MIG slices (fractional GPUs that
    frequently mis-provision), and only types offered in this cloud."""
    price_key = "securePrice" if cloud == "SECURE" else "communityPrice"
    prices = {}
    for g in runpod.get_gpus():
        gid = g["id"]
        if not gid.startswith("NVIDIA") or "MIG" in gid:
            continue
        price = runpod.get_gpu(gid).get(price_key)
        if price:
            prices[gid] = price
    return prices


def pick_gpu_types(prices, max_price, preferred, requested=None):
    """GPU types to try, all at or under max_price per GPU.

    requested (from --gpu-types) keeps the caller's order. Entries that are
    unknown, not offered in this cloud, or over the cap are skipped with a
    warning (never retried until the timeout); if none are left, that's an
    error. Otherwise: every affordable type, cheapest first, with the
    preferred type (RUNPOD_GPU_TYPE) moved to the front so uniform fleets stay
    the default when capacity allows.
    """
    if requested:
        usable, problems = [], []
        for t in requested:
            if t not in prices:
                problems.append(f"{t!r} is unknown or not offered in this cloud")
            elif prices[t] > max_price:
                problems.append(f"{t} costs ${prices[t]:.2f}/hr per GPU, over --max-price {max_price:.2f}")
            else:
                usable.append(t)
        if not usable:
            sys.exit("No usable --gpu-types: " + "; ".join(problems)
                     + ". Known types: " + ", ".join(sorted(prices)))
        for p in problems:
            print(f"Skipping: {p}.")
        return usable
    types = [t for _, t in sorted((p, t) for t, p in prices.items() if p <= max_price)]
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
    ap.add_argument("--data-centers", help="comma-separated RunPod datacenter ids to restrict to, "
                    "e.g. US-NE-1,US-CA-2 (default: any)")
    ap.add_argument("--timeout", type=int, default=1800, help="seconds before giving up")
    args = ap.parse_args()
    # Defaults come from config.env, which argparse doesn't validate.
    args.cloud = (args.cloud or "").upper()
    if args.cloud not in ("COMMUNITY", "SECURE"):
        ap.error(f"cloud must be COMMUNITY or SECURE, got {args.cloud!r} (check RUNPOD_CLOUD_TYPE)")
    if args.gpu_count < 1 or args.disk < 1 or args.volume < 0:
        ap.error("--gpu-count and --disk must be positive, --volume zero or more")
    dcs = [d.strip() for d in args.data_centers.split(",") if d.strip()] if args.data_centers else None

    existing = used_pod_names()
    used_short = {n[len(PREFIX + "-"):] for n in existing if n.startswith(PREFIX + "-")}

    if args.names:
        # Accept names with or without the "<prefix>-" prefix, but only names
        # in MACHINE_NAME_LIST: the proxy routes nothing else, so an unlisted
        # name (usually a typo) would be a pod nobody can reach.
        targets = [to_bare(n) for n in args.names]
        unknown = [n for n in targets if n not in ALLOWED]
        if unknown:
            sys.exit(f"Not in MACHINE_NAME_LIST: {', '.join(unknown)}. The proxy only "
                     "routes listed names; add new names to the END of the list in "
                     "config.env first.")
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

    requested = [t.strip() for t in args.gpu_types.split(",") if t.strip()] if args.gpu_types else None
    preferred = os.getenv("RUNPOD_GPU_TYPE") or "NVIDIA RTX A4000"
    gpu_types = pick_gpu_types(gpu_prices(args.cloud), args.max_price, preferred, requested)
    if not gpu_types:
        print(f"No {args.cloud} GPU types at or below ${args.max_price}/hr. Raise --max-price.")
        sys.exit(1)

    pubkey = read_pubkey()
    print(f"Targets ({len(targets)}): {targets}")
    print(f"{args.gpu_count}x GPU per pod, {args.cloud}, at most ${args.max_price:.2f}/hr per GPU "
          f"(${args.max_price * args.gpu_count:.2f}/hr per pod). Types to try: {gpu_types}")

    deadline = time.time() + args.timeout
    lock = threading.Lock()
    stop = threading.Event()
    results = {}

    def landed(full):
        """True if a pod with this name exists now: after a network error or
        a 5xx, the create may have gone through even though we got no answer."""
        try:
            return any(p.get("name") == full for p in runpod.get_pods())
        except Exception:  # noqa: BLE001
            return False

    def worker(bare):
        full = f"{PREFIX}-{bare}"
        env = {"MACHINE_NAME": bare, "PUBLIC_KEY": pubkey}
        attempt = 0
        # Each worker rotates from its own random starting type, so a fleet
        # doesn't pile onto the same fallback type (and the same bad hosts).
        offset = random.randrange(len(gpu_types))
        while time.time() < deadline and not stop.is_set():
            attempt += 1
            # bias the first few attempts toward the preferred GPU,
            # then rotate through every affordable type.
            gpu = gpu_types[0] if attempt <= 3 else gpu_types[(attempt - 4 + offset) % len(gpu_types)]
            # ONLY the create call may live in this try: if anything after a
            # successful create raised, a retry would create a DUPLICATE pod.
            try:
                res = runpod.create_pod(
                    name=full, image_name=args.image, gpu_count=args.gpu_count,
                    volume_in_gb=args.volume, container_disk_in_gb=args.disk,
                    ports="8888/http,22/tcp", volume_mount_path="/workspace",
                    gpu_type_id=gpu, cloud_type=args.cloud, data_center_id=dcs,
                    docker_args=DOCKER_ARGS, env=env,
                )
            except Exception as e:  # noqa: BLE001
                msg = str(e)
                if runpod.is_capacity_error(e):
                    stop.wait(random.uniform(0.4, 1.2))
                    continue
                status = getattr(e, "status", 0) if isinstance(e, runpod.RunPodError) else None
                retryable = status == 429 or (status or 0) >= 500 or isinstance(e, OSError)
                if not retryable:
                    # RunPod rejected the request itself (bad field, auth, no
                    # credit) or our input is wrong: retrying can't fix it.
                    print(f"[FAILED] {full}: {msg[:300]}", flush=True)
                    return
                if status != 429 and landed(full):
                    with lock:
                        results[bare] = (gpu, "(created; response was lost)")
                    print(f"[OK] {full} <- {gpu} (attempt {attempt}; the create went through)", flush=True)
                    return
                if attempt % 25 == 0:
                    print(f"[..] {full} attempt {attempt}: {msg[:110]}", flush=True)
                stop.wait(random.uniform(0.8, 1.5) * (3 if status == 429 else 1))
                continue
            pod_id = res.get("id") if isinstance(res, dict) else None
            with lock:
                results[bare] = (gpu, pod_id)
            print(f"[OK] {full} <- {gpu} (attempt {attempt}) id={pod_id}", flush=True)
            return
        if not stop.is_set():
            print(f"[TIMEOUT] {full} gave up after {attempt} attempts", flush=True)

    threads = [threading.Thread(target=worker, args=(n,), daemon=True) for n in targets]
    for t in threads:
        t.start()
    try:
        last = time.time()
        while any(t.is_alive() for t in threads):
            time.sleep(0.5)
            if time.time() - last >= 15:
                last = time.time()
                with lock:
                    print(f"--- progress: {len(results)}/{len(targets)} landed: {sorted(results)} ---", flush=True)
    except KeyboardInterrupt:
        stop.set()
        print("\nStopping: letting requests already sent finish...", flush=True)
        for t in threads:
            t.join(timeout=70)   # an in-flight create can take up to the 60s HTTP timeout
        with lock:
            made = sorted(results)
        print(f"Stopped. Created before stopping: {', '.join(PREFIX + '-' + n for n in made) or 'none'}"
              " (destroy_pods <names> to remove them).")
        sys.exit(130)

    print(f"\n=== DONE: {len(results)}/{len(targets)} pods created ===")
    for n, (gpu, pid) in sorted(results.items()):
        print(f"  {PREFIX}-{n}: {gpu}  {pid}")
    if len(results) < len(targets):
        missing = sorted(set(targets) - set(results))
        print(f"\nNot created: {', '.join(missing)}. Re-run the same command to retry just those.")
        sys.exit(1)
    print("\nNext: `ready_pods` — waits for IPs, updates the proxy, checks every pod.")


if __name__ == "__main__":
    main()
