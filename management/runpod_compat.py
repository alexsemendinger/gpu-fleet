"""Drop-in replacement for the `runpod` SDK's pod-management surface, speaking
REST v2 (https://api.runpod.io/v2) instead of GraphQL.

Why: RunPod is decommissioning GraphQL (early 2027) and REST v1 (Nov 2026);
the pip `runpod` SDK (<=1.12) still speaks GraphQL for pod management, so the
fleet scripts import this module instead:

    import runpod_compat as runpod

The public surface mirrors what the scripts used from the SDK — `api_key`,
`get_pods`, `create_pod`, `stop_pod`, `resume_pod`, `terminate_pod`,
`get_gpu`, `get_gpus` — and returns dicts in the *legacy GraphQL shape*
(`desiredStatus`, `costPerHr`, `machine.gpuDisplayName`,
`runtime.ports[].{ip,isIpPublic,publicPort,privatePort,type}`) so call sites
don't need to care which API generation is underneath. Network-volume helpers:
`list_network_volumes`, `create_network_volume`. v2 timestamps are ISO 8601;
`parse_timestamp` also accepts the older '2026-07-22 14:30:40 +0000 UTC' form.

v2 semantics that differ from the old SDK (handled here, but worth knowing):
  - `startSsh` defaults to FALSE in v2 (SDK defaulted true) — always sent.
  - `docker_args` needs NO quote-escaping (the SDK's GraphQL f-string did).
  - No `cloud_type="ALL"`: v2 requires SECURE or COMMUNITY (raises).
  - `min_vcpu_count` / `min_memory_in_gb` are per-POD totals in the SDK but
    per-GPU placement filters in v2 (`gpu.minVcpuCountPerGpu` /
    `gpu.minRamPerGpu`); the shim divides by gpu_count, rounding up.
  - `support_public_ip` has no v2 equivalent; accepted and ignored.
  - Errors are RFC 9457; RunPodError.message carries title + detail so
    substring matching (e.g. burst_create's availability retry) still works.
"""
import ipaddress
import json
import urllib.error
import urllib.request
from datetime import datetime

API = "https://api.runpod.io/v2"

# Set by callers, exactly like `runpod.api_key`.
api_key = None


class RunPodError(Exception):
    def __init__(self, status, title, detail, path):
        self.status, self.title, self.detail, self.path = status, title, detail, path
        super().__init__(f"HTTP {status} {title}: {detail} [{path}]")


# Substrings of create errors that mean "no capacity for this request right
# now": retry, ideally with another GPU type. Anything else is a real error.
CAPACITY_ERRORS = ("no longer any instances", "no instances available",
                   "does not have the resources", "insufficient capacity",
                   "no capacity", "unavailable")


def is_capacity_error(exc):
    return any(k in str(exc).lower() for k in CAPACITY_ERRORS)


def _call(method, path, body=None, timeout=60):
    if not api_key:
        raise RunPodError(0, "config", "runpod_compat.api_key is not set", path)
    data = json.dumps(body).encode() if body is not None else None
    # Cloudflare in front of api.runpod.io rejects urllib's default
    # Python-urllib UA with 403 error 1010; any explicit UA passes.
    headers = {"Authorization": "Bearer " + api_key,
               "User-Agent": "gpu-fleet-runpod-compat/1.0"}
    if data:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(API + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            return json.loads(raw) if raw.strip() else {}
    except urllib.error.HTTPError as e:
        raw = e.read().decode(errors="replace")[:2000]
        try:
            prob = json.loads(raw)
            title = prob.get("title", "error")
            detail = prob.get("detail", raw)
            if "errors" in prob:  # typed 422 field errors
                detail += " " + json.dumps(prob["errors"])
        except (ValueError, AttributeError):
            title, detail = "error", raw
        raise RunPodError(e.code, title, detail, path) from None


def _is_public_ip(ip):
    try:
        return ipaddress.ip_address(ip).is_global
    except ValueError:
        return False  # hostnames (ssh proxy) are not direct public IPs


# GPU catalog cache: v2 pods carry gpu.id (e.g. "NVIDIA RTX A4000"); the
# legacy machine.gpuDisplayName (e.g. "RTX A4000") comes from the catalog.
_gpu_catalog = None


def _catalog():
    global _gpu_catalog
    if _gpu_catalog is None:
        _gpu_catalog = {g["id"]: g for g in _call("GET", "/catalog/gpus").get("gpus", [])}
    return _gpu_catalog


# v2 PROVISIONING/STARTING map to legacy RUNNING-with-null-runtime: GraphQL
# reported desiredStatus=RUNNING from creation, with `runtime` filling in
# once the pod was actually up, and the billing/kill logic keys off that.
_STATUS = {"PROVISIONING": "RUNNING", "STARTING": "RUNNING"}


def _legacy_pod(p):
    gpu = p.get("gpu") or {}
    gpu_id = gpu.get("id")
    # Display name only: a catalog failure must never turn a pod we already
    # have (e.g. one just created) into an exception.
    try:
        cat = _catalog().get(gpu_id) if gpu_id else None
    except Exception:  # noqa: BLE001
        cat = None
    display = (cat or {}).get("name") or gpu_id

    runtime = None
    v2rt = p.get("runtime")
    if v2rt:
        ports = []
        for pt in v2rt.get("ports") or []:
            ip = pt.get("ip")
            ports.append({
                "ip": ip,
                "isIpPublic": bool(ip) and pt.get("public") is not None and _is_public_ip(ip),
                "publicPort": pt.get("public"),
                "privatePort": pt.get("private"),
                "type": pt.get("type"),
            })
        runtime = {"ports": ports, "uptimeInSeconds": v2rt.get("uptime")}

    status = p.get("status", "")
    return {
        "id": p.get("id"),
        "name": p.get("name"),
        "desiredStatus": _STATUS.get(status, status),
        "costPerHr": p.get("cost"),
        "gpuCount": gpu.get("count"),
        "machine": {"gpuDisplayName": display},
        "runtime": runtime,
        "createdAt": p.get("createdAt"),
        "startedAt": p.get("startedAt"),
        "imageName": p.get("image"),
        "env": p.get("env"),
        "_v2": p,  # full v2 object for callers that want the new fields
    }


def parse_timestamp(s):
    """Parse both v2 ISO 8601 and legacy '2026-07-22 14:30:40.116 +0000 UTC'."""
    if not s:
        return None
    s = s.replace(" UTC", "").strip()
    for fmt in ("%Y-%m-%d %H:%M:%S.%f %z", "%Y-%m-%d %H:%M:%S %z"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def get_pods():
    return [_legacy_pod(p) for p in _call("GET", "/pods").get("pods", [])]


def get_pod(pod_id):
    return _legacy_pod(_call("GET", f"/pods/{pod_id}"))


_IGNORED_CREATE_KWARGS = {"support_public_ip", "min_download", "min_upload",
                          "start_jupyter"}


def create_pod(name, image_name, gpu_type_id, cloud_type="SECURE", gpu_count=1,
               container_disk_in_gb=None, volume_in_gb=None, volume_mount_path=None,
               network_volume_id=None, data_center_id=None, ports=None, env=None,
               docker_args=None, template_id=None, allowed_cuda_versions=None,
               min_vcpu_count=None, min_memory_in_gb=None, start_ssh=True,
               **ignored):
    """Legacy-SDK-shaped create. docker_args must be UNESCAPED (no GraphQL
    quote-doubling). Returns the created pod in legacy shape."""
    unknown = set(ignored) - _IGNORED_CREATE_KWARGS
    if unknown:
        raise TypeError(f"create_pod: unsupported kwargs {sorted(unknown)}")
    if cloud_type not in ("SECURE", "COMMUNITY"):
        raise ValueError(f"v2 has no cloud_type={cloud_type!r}; use SECURE or COMMUNITY")

    body = {
        "name": name,
        "image": image_name,
        "cloud": cloud_type,
        "gpu": {"id": gpu_type_id, "count": gpu_count},
        "startSsh": start_ssh,
    }
    if allowed_cuda_versions:
        body["gpu"]["allowedCudaVersions"] = list(allowed_cuda_versions)
    if min_vcpu_count:
        body["gpu"]["minVcpuCountPerGpu"] = -(-min_vcpu_count // gpu_count)
    if min_memory_in_gb:
        body["gpu"]["minRamPerGpu"] = -(-min_memory_in_gb // gpu_count)
    if container_disk_in_gb:
        body["disk"] = container_disk_in_gb
    mounts = {}
    if network_volume_id:
        mounts["network"] = [{"volumeId": network_volume_id,
                              "path": volume_mount_path or "/workspace"}]
    elif volume_in_gb and volume_mount_path:
        mounts["persistent"] = {"size": volume_in_gb, "path": volume_mount_path}
    if mounts:
        body["mounts"] = mounts
    if data_center_id:
        body["dataCenterIds"] = ([data_center_id] if isinstance(data_center_id, str)
                                 else list(data_center_id))
    if ports:
        body["ports"] = ports.split(",") if isinstance(ports, str) else list(ports)
    if env:
        body["env"] = env
    if docker_args:
        body["args"] = docker_args
    if template_id:
        body["templateId"] = template_id
    return _legacy_pod(_call("POST", "/pods", body))


def _action(pod_id, action):
    return _call("POST", f"/pods/{pod_id}/action", {"action": action})


def stop_pod(pod_id):
    return _action(pod_id, "stop")


def resume_pod(pod_id, gpu_count=None):
    return _action(pod_id, "start")


def terminate_pod(pod_id):
    return _action(pod_id, "terminate")


def get_gpus():
    """Legacy list shape: [{id, displayName, memoryInGb}]."""
    return [{"id": g["id"], "displayName": g["name"], "memoryInGb": g["memory"]}
            for g in _catalog().values()]


def get_gpu(gpu_id):
    """Legacy shape incl. securePrice/communityPrice (provision_request)."""
    g = _catalog().get(gpu_id)
    if g is None:
        raise RunPodError(404, "not found", f"no GPU type {gpu_id!r} in catalog", "/catalog/gpus")
    price = g.get("price") or {}
    return {"id": g["id"], "displayName": g["name"], "memoryInGb": g["memory"],
            "securePrice": price.get("secure"), "communityPrice": price.get("community"),
            "secureCloud": g.get("secure"), "communityCloud": g.get("community"),
            "availability": g.get("availability"), "dataCenters": g.get("dataCenters")}


def list_network_volumes():
    """Legacy field names: dataCenterId (v2 calls it dataCenter)."""
    return [{"id": v["id"], "name": v["name"], "size": v["size"],
             "dataCenterId": v.get("dataCenter")}
            for v in _call("GET", "/network-volumes").get("networkVolumes", [])]


def create_network_volume(name, size, data_center_id):
    v = _call("POST", "/network-volumes",
              {"name": name, "size": size, "dataCenter": data_center_id})
    return {"id": v["id"], "name": v["name"], "size": v["size"],
            "dataCenterId": v.get("dataCenter")}
