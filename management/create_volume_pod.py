#!/usr/bin/env python3
"""Create a RunPod pod backed by a PERSISTENT NETWORK VOLUME.

WHY THIS EXISTS
---------------
The normal fleet (create_pods.py) uses COMMUNITY
pods with an *ephemeral* container disk: when the pod is destroyed, its data
is gone. Some pods need data to survive a nuke + recreate (training
checkpoints, datasets, long runs). RunPod's answer is a **network volume** —
a persistent disk that lives in one datacenter and can be re-attached to a
fresh pod later.

Two hard facts about network volumes drive everything here:
  1. A network volume is REGION-LOCKED. The pod must be created in the SAME
     datacenter as its volume (data_center_id == volume's dataCenterId).
  2. Network volumes only exist in datacenters with storage support, and only
     work with SECURE-cloud (datacenter) pods, not community pods.

WHAT THIS DOES
--------------
  * Finds-or-creates a network volume named "<prefix>-<name>-vol" (idempotent:
    if a volume with that name already exists it is REUSED, which is exactly
    what you want when recreating a pod on its old data).
  * Creates a SECURE on-demand pod in the volume's datacenter, attaches the
    volume at --mount (default /workspace), with the requested GPU / vCPU / RAM.
  * Records the pod -> volume mapping in network_volumes.json so we always know
    which volume (and which datacenter) a pod must be recreated against.

RECREATE-ON-SAME-VOLUME
-----------------------
Just run the same command again with the same pod name. The volume is matched
by name and reused; the registry already has the datacenter. Nothing to
remember by hand. (You can also pass --volume-id explicitly to pin one.)

USAGE
-----
  # H100 SXM pod, 200 GB persistent volume, 16 vCPU / 64 GB RAM:
  create_volume_pod oak \
      --gpu-type "NVIDIA H100 80GB HBM3" --data-center US-NE-1 \
      --volume-size 200 --vcpu 16 --memory 64

  create_volume_pod oak               # recreate: reuses gpu-oak-vol

See NETWORK_VOLUMES.md for the full walk-through and how to pick a datacenter.
"""
import argparse
import json
import os

from mydotenv import load_env
load_env()
import runpod_compat as runpod
from pod_boot import runpod_start_args

from pod_names import to_bare, to_full

runpod.api_key = os.getenv("RUNPOD_API_KEY")
PREFIX = os.environ["MACHINE_NAME_PREFIX"]
IMAGE = os.environ["RUNPOD_DOCKER_IMAGE"]
REGISTRY = os.path.join(os.path.dirname(os.path.abspath(__file__)), "network_volumes.json")

# Optional boot hook (POD_SETUP_CMD in config.env); None boots the image as-is.
DOCKER_ARGS = runpod_start_args()


def read_pubkey():
    p = os.getenv("SHARED_SSH_KEY_PATH")
    if not p:
        return ""
    try:
        return open(os.path.expanduser(p + ".pub")).read().strip()
    except FileNotFoundError:
        print(f"WARNING: SSH public key not found at {p}.pub")
        return ""


def find_or_create_volume(vol_name, size, data_center):
    """Return (volume_id, data_center_id). Reuse an existing volume with this
    name (region-locked, so its datacenter wins); else create a new one."""
    for v in runpod.list_network_volumes():
        if v.get("name") == vol_name:
            dc = v.get("dataCenterId")
            print(f"Reusing existing volume '{vol_name}' id={v['id']} "
                  f"({v.get('size')}GB, {dc})")
            if data_center and dc != data_center:
                print(f"  NOTE: existing volume is in {dc}; --data-center "
                      f"{data_center} ignored (volumes are region-locked).")
            return v["id"], dc
    if not data_center:
        raise SystemExit("No existing volume and no --data-center given; "
                         "cannot choose a region. Pass --data-center.")
    print(f"Creating volume '{vol_name}' ({size}GB) in {data_center}...")
    v = runpod.create_network_volume(vol_name, size, data_center)
    print(f"  created id={v['id']}")
    return v["id"], v["dataCenterId"]


def record(bare, full, vol_id, vol_name, dc, size, gpu, cloud, mount,
           vcpu=None, memory=None, disk=None, gpu_count=1, note=None):
    reg = {}
    if os.path.exists(REGISTRY):
        reg = json.load(open(REGISTRY))
    # recreate_cmd carries every flag not derivable from the (region-locked,
    # name-matched) volume, so pasting it rebuilds the pod faithfully.
    cmd = (f'create_volume_pod {bare} --gpu-type "{gpu}" '
           f'--gpu-count {gpu_count} --vcpu {vcpu} --memory {memory} '
           f'--disk {disk}')
    reg[full] = {
        "network_volume_id": vol_id, "network_volume_name": vol_name,
        "data_center_id": dc, "volume_size_gb": size, "gpu_type": gpu,
        "gpu_count": gpu_count, "cloud_type": cloud, "mount_path": mount,
        "min_vcpu": vcpu, "min_memory_gb": memory, "container_disk_gb": disk,
        "note": note,
        "recreate_cmd": cmd,
    }
    json.dump(reg, open(REGISTRY, "w"), indent=2, sort_keys=True)
    open(REGISTRY, "a").write("\n")
    print(f"Recorded {full} -> volume {vol_id} in {REGISTRY}")


def main():
    ap = argparse.ArgumentParser(description="Create a RunPod pod on a persistent network volume")
    ap.add_argument("name", help="bare pod name, e.g. oak")
    ap.add_argument("--gpu-type", default="NVIDIA H100 80GB HBM3")
    ap.add_argument("--gpu-count", type=int, default=1)
    ap.add_argument("--data-center", help="e.g. US-NE-1 (required for a new volume)")
    ap.add_argument("--volume-size", type=int, default=200, help="GB (new volume only)")
    ap.add_argument("--volume-id", help="pin an existing volume id (skips name lookup)")
    ap.add_argument("--vcpu", type=int, default=16, help="min vCPUs for the pod")
    ap.add_argument("--memory", type=int, default=64, help="min RAM GB")
    ap.add_argument("--disk", type=int, default=100, help="ephemeral container disk GB")
    ap.add_argument("--cloud-type", default="SECURE", choices=["SECURE", "COMMUNITY"])
    ap.add_argument("--mount", default="/workspace", help="volume mount path")
    args = ap.parse_args()

    bare = to_bare(args.name)
    full = to_full(bare)
    vol_name = f"{full}-vol"

    # Guard against duplicates: bail if the pod is already up.
    for p in runpod.get_pods() or []:
        if p.get("name") == full:
            raise SystemExit(f"{full} already exists (id={p.get('id')}). "
                             f"Nuke it first if you mean to recreate.")

    if args.volume_id:
        vol_id = args.volume_id
        dc = next((v.get("dataCenterId") for v in runpod.list_network_volumes()
                   if v["id"] == vol_id), None)
        if not dc:
            raise SystemExit(f"volume id {vol_id} not found")
    else:
        vol_id, dc = find_or_create_volume(vol_name, args.volume_size, args.data_center)

    pubkey = read_pubkey()
    env = {"MACHINE_NAME": bare}
    if pubkey:
        env["PUBLIC_KEY"] = pubkey

    print(f"\nCreating pod {full}: {args.gpu_count}x {args.gpu_type} in {dc}, "
          f"{args.cloud_type}, {args.vcpu} vCPU / {args.memory}GB RAM, "
          f"volume {vol_id} @ {args.mount}")
    res = runpod.create_pod(
        name=full, image_name=IMAGE, gpu_type_id=args.gpu_type,
        gpu_count=args.gpu_count,
        cloud_type=args.cloud_type, data_center_id=dc,
        network_volume_id=vol_id, volume_mount_path=args.mount,
        volume_in_gb=0, container_disk_in_gb=args.disk,
        min_vcpu_count=args.vcpu, min_memory_in_gb=args.memory,
        start_ssh=True,
        ports="8888/http,22/tcp", docker_args=DOCKER_ARGS, env=env,
    )
    pod_id = res.get("id") if isinstance(res, dict) else None
    print(f"  created pod id={pod_id}")
    record(bare, full, vol_id, vol_name, dc, args.volume_size, args.gpu_type,
           args.cloud_type, args.mount, args.vcpu, args.memory, args.disk,
           args.gpu_count)
    print("\nNext: `ready_pods " + bare + "` (waits for the IP, updates the proxy, checks the pod).")


if __name__ == "__main__":
    main()
