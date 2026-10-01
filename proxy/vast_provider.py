#!/usr/bin/env python3
"""Vast.ai provider helper for the proxy box -- stdlib only (no SDK).

Mirrors management/vast_provider.py but uses urllib + the Vast REST API, the
same dependency-free approach nginx_pods.py already uses for RunPod's GraphQL.
The proxy box stays free of pip dependencies.

Vast REST: GET https://console.vast.ai/api/v0/instances?owner=me
           Authorization: Bearer <VASTAI_API_KEY>
           -> {"instances": [ {...}, ... ]}
"""
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request

from mydotenv import load_env
load_env()

VAST_BASE = os.getenv("VAST_URL", "https://console.vast.ai")


def _extract_ip_port(inst):
    """Return (ip, port) preferring the DIRECT public endpoint.

    Same precedence as the SDK twin: direct public_ipaddr + host port mapped
    to container 22 (a clean nginx upstream), else Vast's SSH proxy.
    """
    ports = inst.get("ports") or {}
    mapping = ports.get("22/tcp") or []
    if isinstance(mapping, list) and mapping:
        host_port = mapping[0].get("HostPort")
        public_ip = (inst.get("public_ipaddr") or "").strip()
        if host_port and public_ip:
            return public_ip, str(host_port).strip()

    ssh_host = (inst.get("ssh_host") or "").strip()
    ssh_port = inst.get("ssh_port")
    if ssh_host and ssh_port:
        return ssh_host, str(ssh_port).strip()

    return None, None


def normalize_instance(inst):
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



def _redact(err):
    """Error text with any API key blanked: the vastai SDK puts the key in the
    request URL, and HTTP errors echo that URL."""
    return re.sub(r"(api_key=)[^&\s'\"]+", r"\1<redacted>", str(err))


def get_vast_pods():
    """Return normalized pods for all Vast instances on this account.

    Returns [] (with a warning) on any failure so a Vast outage never breaks
    the RunPod proxy config generation.
    """
    api_key = os.getenv("VASTAI_API_KEY")
    if not api_key:
        print("# Warning: VASTAI_API_KEY not set; skipping Vast.ai instances")
        return []

    query = urllib.parse.urlencode({"owner": "me", "api_key": api_key})
    url = f"{VAST_BASE}/api/v0/instances?{query}"
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Accept": "application/json",
            "User-Agent": "gpu-fleet-proxy",
        },
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        print(f"# Warning: Vast.ai HTTP {e.code}: {body[:200]}")
        return []
    except Exception as e:  # noqa: BLE001
        print(f"# Warning: failed to fetch Vast.ai instances: {_redact(e)}")
        return []

    instances = data.get("instances") or []
    pods = []
    for inst in instances:
        try:
            pods.append(normalize_instance(inst))
        except Exception as e:  # noqa: BLE001
            print(f"# Warning: failed to normalize a Vast instance: {e}")
    return pods


if __name__ == "__main__":
    for p in sorted(get_vast_pods(), key=lambda x: x["name"] or ""):
        print(p)
