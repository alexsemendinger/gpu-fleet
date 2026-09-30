#!/usr/bin/env python3
"""Create Vast.ai instances that slot into the existing nginx proxy workflow.

Mirrors create_new_pods.py (-n / -a / explicit names) but provisions on
Vast.ai instead of RunPod. Each instance is created with:

  * the same Docker image as RunPod (VASTAI_DOCKER_IMAGE),
  * label == full pod name ("gpu-alder"), the stable identity that
    list_pods.py / nginx_pods.py match on,
  * runtype "ssh_direc ssh_proxy" (== `vastai create instance --ssh --direct`),
    so the instance gets a direct public IP:port the nginx proxy can reach,
  * an onstart script (set -e, echoed steps) that installs the shared SSH
    public key into authorized_keys and writes /root/.name.

Vast's marketplace `offer_id` is ephemeral and only used at creation time;
the durable key is the label, so nothing needs to track offer ids over time.
"""
import argparse
import ast
import os
import sys
import time

from mydotenv import load_env
load_env()

from vast_provider import get_vast_client, get_vast_pods
from pod_names import to_bare

RUNTYPE = "ssh_direc ssh_proxy"  # == `vastai create instance --ssh --direct`


def explain_error(e):
    """Surface the Vast.ai error body, not just '400 Client Error'.

    Vast returns a JSON body like {"error":"insufficient_credit","msg":...};
    the SDK raises a bare requests.HTTPError that hides it. Pull it out so
    failures are diagnosable in seconds.
    """
    resp = getattr(e, "response", None)
    if resp is not None:
        try:
            body = resp.json()
            detail = body.get("msg") or body.get("error") or resp.text
            err = body.get("error")
            return f"{err}: {detail}" if err and err not in str(detail) else str(detail)
        except Exception:  # noqa: BLE001
            return resp.text or str(e)
    return str(e)


def read_public_key():
    """Read the shared SSH public key (same key participants use)."""
    ssh_key_path = os.getenv("SHARED_SSH_KEY_PATH")
    if not ssh_key_path:
        print("Error: SHARED_SSH_KEY_PATH not set in config.env")
        sys.exit(1)
    pub_path = ssh_key_path + ".pub"
    if pub_path.startswith("~"):
        pub_path = os.path.expanduser(pub_path)
    try:
        with open(pub_path) as f:
            key = f.read().strip()
    except FileNotFoundError:
        print(f"Error: SSH public key not found at {pub_path}")
        print("Regenerate it with: ssh-keygen -y -f <private_key> > <private_key>.pub")
        sys.exit(1)
    if "'" in key:
        # Public keys never contain single quotes; bail rather than emit a
        # broken onstart script.
        print("Error: public key contains a single quote; refusing to inline it.")
        sys.exit(1)
    print(f"Loaded SSH public key from: {pub_path}")
    return key


def build_onstart(machine_name, public_key):
    """Onstart script: set -e + echoed steps so SSH-key setup and the
    optional POD_SETUP_CMD hook are visible in Vast's instance logs and
    diagnosable in seconds."""
    setup = (os.getenv("POD_SETUP_CMD") or "").strip()
    setup_block = (f"echo '[onstart] running POD_SETUP_CMD'\n({setup}) || true\n"
                   if setup else "")
    return f"""#!/bin/bash
set -e
echo '[onstart] START'
echo '[onstart] ensuring /root/.ssh'
mkdir -p /root/.ssh
chmod 700 /root/.ssh
echo '[onstart] installing shared SSH key into authorized_keys'
touch /root/.ssh/authorized_keys
KEY='{public_key}'
grep -qF "$KEY" /root/.ssh/authorized_keys || echo "$KEY" >> /root/.ssh/authorized_keys
chmod 600 /root/.ssh/authorized_keys
echo '[onstart] writing /root/.name (MACHINE_NAME={machine_name})'
echo "export MACHINE_NAME='{machine_name}'" > /root/.name
{setup_block}echo '[onstart] DONE'
"""


def pick_offers(client, query, max_price, count, storage):
    """Return up to `count` cheapest distinct offers with dph_total <= max_price.

    Returns fewer offers if supply is short; caller is responsible for noting
    the shortfall. Exits only if zero offers match the query at all.
    """
    print(f"Searching offers: '{query}' (order: cheapest first, cap ${max_price}/hr)")
    offers = client.search_offers(query=query, order="dph_total", storage=storage)
    if not offers:
        print("Error: no Vast.ai offers matched the search query at all.")
        print("Loosen VASTAI_GPU_NAME / VASTAI_SEARCH_QUERY and retry.")
        sys.exit(1)

    affordable = [o for o in offers if o.get("dph_total") is not None
                  and o["dph_total"] <= max_price]
    if len(affordable) < count:
        cheapest = offers[0].get("dph_total")
        short = count - len(affordable)
        print(f"Warning: need {count} offer(s) at or below ${max_price}/hr, "
              f"found only {len(affordable)}. Will create {len(affordable)}; "
              f"{short} more still needed.")
        print(f"Cheapest available offer is ${cheapest}/hr. "
              f"Raise --max-price / VASTAI_MAX_PRICE if you want more.")

    chosen = affordable[:count]
    for o in chosen:
        print(f"  offer {o['id']}: {o.get('gpu_name')} x{o.get('num_gpus')} "
              f"@ ${o['dph_total']}/hr ({o.get('geolocation', '?')})")
    return chosen


def wait_until_running(client, instance_id, timeout=600, interval=10):
    """Poll show_instance until actual_status == 'running' (or timeout)."""
    print(f"  waiting for instance {instance_id} to boot (timeout {timeout}s)...")
    start = time.time()
    while time.time() - start < timeout:
        try:
            inst = client.show_instance(id=instance_id)
        except Exception as e:  # noqa: BLE001
            print(f"  (transient) show_instance failed: {e}")
            time.sleep(interval)
            continue
        status = inst.get("actual_status") or inst.get("cur_state")
        elapsed = int(time.time() - start)
        print(f"  [{elapsed:>3}s] status={status}")
        if status == "running":
            return inst
        time.sleep(interval)
    print(f"  WARNING: instance {instance_id} not 'running' after {timeout}s; "
          f"continuing (check `list_pods`).")
    return None


def get_used_pod_names(client=None):
    """Return full pod names in use across BOTH providers (Vast + RunPod).

    Best-effort per provider: a failure on one side doesn't hide the other.
    Used to prevent name collisions when create_vast_pods runs alongside
    RunPod pods that share the same nginx proxy and SSH config.
    """
    used = set()
    try:
        for pod in get_vast_pods(client):
            n = pod.get("name")
            if n:
                used.add(n)
    except Exception as e:
        print(f"# Warning: failed to fetch Vast pods for collision check: {e}")
    try:
        import runpod_compat as runpod
        api_key = os.getenv("RUNPOD_API_KEY")
        if api_key:
            runpod.api_key = api_key
            for pod in runpod.get_pods() or []:
                n = pod.get("name")
                if n:
                    used.add(n)
    except Exception as e:
        print(f"# Warning: failed to fetch RunPod pods for collision check: {e}")
    return used


def resolve_machine_names(args, prefix, allowed, used_full_names):
    """Mirror create_new_pods.py name selection, cross-provider-aware."""
    if args.machine_names:
        # Accept names with or without the "<prefix>-" prefix; we work in bare
        # names and re-add the prefix when labelling the instance.
        names = [to_bare(n) for n in args.machine_names]
        missing = [n for n in names if n not in allowed]
        if missing:
            print("--------------------------------")
            print(f"WARNING: Machine names {missing} not in allowed MACHINE_NAME_LIST.")
            print("- Creation works, but proxy/ssh-config scripts may skip them.")
            print("--------------------------------")
        return names
    if args.num_machines:
        # Downstream skip filters out names already in use on either provider,
        # preserving create_new_pods.py's 'ensure N total' semantic.
        return allowed[:args.num_machines]
    if args.add:
        used_short = {n[len(prefix + "-"):] for n in used_full_names
                      if n.startswith(prefix + "-")}
        unused = [n for n in allowed if n not in used_short]
        if len(unused) < args.add:
            print(f"Error: cannot add {args.add}; only {len(unused)} unused "
                  f"machine names available. Used: {sorted(used_short)}")
            sys.exit(1)
        chosen = unused[:args.add]
        print(f"Adding {args.add} machines (have {len(used_short)}): {chosen}")
        return chosen
    print(f"Using default list with {len(allowed)} machines")
    return allowed[:]


def main():
    parser = argparse.ArgumentParser(description="Create Vast.ai instances")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("-n", "--num-machines", type=int,
                       help="Total machines to create from MACHINE_NAME_LIST")
    group.add_argument("-a", "--add", type=int,
                       help="Additional machines to add to existing ones")
    group.add_argument("machine_names", nargs="*", default=[],
                       help="Specific machine names to create")
    parser.add_argument("--gpu-name", help="Vast gpu_name token (overrides VASTAI_GPU_NAME)")
    parser.add_argument("--num-gpus", type=int, help="GPUs per instance (overrides VASTAI_NUM_GPUS)")
    parser.add_argument("--disk", type=int, help="Disk GB (overrides VASTAI_DISK_SPACE_IN_GB)")
    parser.add_argument("--image", help="Docker image (overrides VASTAI_DOCKER_IMAGE)")
    parser.add_argument("--max-price", type=float,
                        help="Max $/hr per offer (overrides VASTAI_MAX_PRICE, default 0.60)")
    args = parser.parse_args()

    prefix = os.environ["MACHINE_NAME_PREFIX"]
    allowed = ast.literal_eval(os.environ["MACHINE_NAME_LIST"])

    gpu_name = args.gpu_name or os.getenv("VASTAI_GPU_NAME", "RTX_A4000")
    num_gpus = args.num_gpus or int(os.getenv("VASTAI_NUM_GPUS", "1"))
    disk = args.disk or int(os.getenv("VASTAI_DISK_SPACE_IN_GB", "100"))
    image = args.image or os.getenv("VASTAI_DOCKER_IMAGE") \
        or os.getenv("RUNPOD_DOCKER_IMAGE", "runpod/pytorch:2.8.0-py3.11-cuda12.8.1-cudnn-devel-ubuntu22.04")
    max_price = args.max_price if args.max_price is not None \
        else float(os.getenv("VASTAI_MAX_PRICE", "0.60"))
    query = os.getenv("VASTAI_SEARCH_QUERY") \
        or f"gpu_name={gpu_name} num_gpus={num_gpus} verified=True rentable=True"

    public_key = read_public_key()
    client = get_vast_client()
    used_full_names = get_used_pod_names(client)

    machine_names = resolve_machine_names(args, prefix, allowed, used_full_names)

    # Skip names whose pod already exists on EITHER provider (avoid collisions).
    pods_to_create, skipped = [], []
    for name in machine_names:
        pod_name = f"{prefix}-{name}"
        (skipped if pod_name in used_full_names else pods_to_create).append((name, pod_name))

    if skipped:
        print("\nThe following pod names already exist on RunPod or Vast (skipping):")
        for _, pod_name in skipped:
            print(f"  - {pod_name}")
    if not pods_to_create:
        print("\nNo new Vast instances to create.")
        return

    print("\nConfiguration:")
    print(f"  Image:     {image}")
    print(f"  GPU:       {gpu_name} x{num_gpus}")
    print(f"  Disk:      {disk} GB")
    print(f"  Max price: ${max_price}/hr")
    print(f"  Runtype:   {RUNTYPE}")
    print("\nThe following Vast instances will be created:")
    for _, pod_name in pods_to_create:
        print(f"  - {pod_name}")

    if input("\nProceed with creation? (y/N): ").lower() != "y":
        print("Aborting.")
        return

    offers = pick_offers(client, query, max_price, len(pods_to_create), disk)

    created, errors = 0, 0
    for (machine_name, pod_name), offer in zip(pods_to_create, offers):
        try:
            print(f"\nCreating '{pod_name}' on offer {offer['id']} "
                  f"(${offer['dph_total']}/hr)...")
            result = client.create_instance(
                id=offer["id"],
                image=image,
                disk=disk,
                label=pod_name,
                runtype=RUNTYPE,
                env={"MACHINE_NAME": machine_name, "PUBLIC_KEY": public_key},
                onstart_cmd=build_onstart(machine_name, public_key),
            )
            instance_id = (result.get("new_contract")
                           or result.get("id")
                           or (result.get("instances") or {}).get("id"))
            if not result.get("success", True) or not instance_id:
                print(f"  ERROR: Vast did not return an instance id: {result}")
                errors += 1
                continue
            print(f"  ✓ created instance {instance_id} (label={pod_name})")
            wait_until_running(client, instance_id)
            created += 1
            time.sleep(2)
        except Exception as e:  # noqa: BLE001
            print(f"  ERROR creating '{pod_name}': {explain_error(e)}")
            errors += 1

    print("\n--- Vast Creation Summary ---")
    print(f"Requested:        {len(machine_names)}")
    print(f"Skipped (exists): {len(skipped)}")
    print(f"Created:          {created}")
    print(f"Errors:           {errors}")
    print("\nRun `list_pods` to see status, then `update_proxy` on the proxy box.")


if __name__ == "__main__":
    main()
