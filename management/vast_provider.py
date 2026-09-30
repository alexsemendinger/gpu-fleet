#!/usr/bin/env python3
"""Vast.ai provider helper (SDK-based).

Normalizes Vast.ai instances into the same shape the RunPod scripts already
iterate over, so list_pods.py / nginx_pods.py can treat both providers
uniformly. The stable per-pod identity is the Vast `label`, which we set to
the full pod name (e.g. "gpu-alder") at creation time -- the exact
analogue of RunPod's pod `name`.

Normalized pod dict:
    {
        "name": "gpu-alder",   # == Vast label == RunPod name
        "ip": "1.2.3.4",            # direct public IP (preferred) or None
        "ssh_port": "40123",        # host port mapped to container 22, or None
        "status": "running",        # Vast actual_status
        "cost_per_hr": 0.42,
        "gpu": "RTX A4000",
        "num_gpus": 1,
        "image": "nickypro/arena-env:6.1",
        "provider": "vast",
        "id": 1234567,              # Vast instance id (ephemeral across recreate)
    }
"""
import os
import warnings

from mydotenv import load_env
load_env()

# show_instances() is deprecated in favour of a paginated v1 API, but the
# v0 shape (label, ports, extra_env) is exactly what we normalize here.
warnings.filterwarnings("ignore", message=r".*show_instances\(\) is deprecated.*")


def get_vast_client():
    """Return an authenticated VastAI client.

    Prefers the VASTAI_API_KEY from config.env so behaviour matches the rest
    of the repo; falls back to the SDK's own resolution (`vastai set api-key`).
    """
    from vastai import VastAI
    api_key = os.getenv("VASTAI_API_KEY")
    return VastAI(api_key=api_key) if api_key else VastAI()


def _extract_ip_port(inst):
    """Return (ip, port) for SSH, preferring the DIRECT public endpoint.

    With runtype "ssh_direc ssh_proxy", `ssh_host`/`ssh_port` point at Vast's
    SSH *proxy*, which the nginx stream proxy should not chain through.
    The direct path is `public_ipaddr` + the host port bound to container 22,
    which mirrors how RunPod exposes its public tcp port. Fall back to the
    Vast proxy only if no direct mapping is available yet.
    """
    ports = inst.get("ports") or {}
    mapping = ports.get("22/tcp") or []
    if isinstance(mapping, list) and mapping:
        host_port = mapping[0].get("HostPort")
        public_ip = (inst.get("public_ipaddr") or "").strip()
        if host_port and public_ip:
            return public_ip, str(host_port).strip()

    # Fallback: Vast SSH proxy (works for connectivity, but not ideal as an
    # nginx upstream). Surfaced so list_pods still shows a usable endpoint.
    ssh_host = (inst.get("ssh_host") or "").strip()
    ssh_port = inst.get("ssh_port")
    if ssh_host and ssh_port:
        return ssh_host, str(ssh_port).strip()

    return None, None


def normalize_instance(inst):
    """Convert one raw Vast instance dict into the normalized pod shape."""
    ip, port = _extract_ip_port(inst)
    return {
        "name": (inst.get("label") or "").strip() or None,
        "ip": ip,
        "ssh_port": port,
        "status": inst.get("actual_status") or inst.get("cur_state") or "N/A",
        "cost_per_hr": inst.get("dph_total"),
        "gpu": inst.get("gpu_name") or "N/A",
        "num_gpus": inst.get("num_gpus") or 0,
        "image": inst.get("image_uuid") or inst.get("image") or "N/A",
        "provider": "vast",
        "id": inst.get("id"),
    }


def get_vast_pods(client=None):
    """Return normalized pods for all of this account's Vast instances.

    Returns [] (and prints a warning) on any auth/API failure so a Vast
    outage never breaks the RunPod path in the callers.
    """
    try:
        client = client or get_vast_client()
        instances = client.show_instances()
    except Exception as e:  # noqa: BLE001 - never let Vast break RunPod listing
        print(f"# Warning: failed to fetch Vast.ai instances: {e}")
        return []

    pods = []
    for inst in instances or []:
        try:
            pods.append(normalize_instance(inst))
        except Exception as e:  # noqa: BLE001
            print(f"# Warning: failed to normalize a Vast instance: {e}")
    return pods


if __name__ == "__main__":
    for p in sorted(get_vast_pods(), key=lambda x: x["name"] or ""):
        print(p)
