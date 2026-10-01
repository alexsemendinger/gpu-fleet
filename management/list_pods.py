#!/usr/bin/env python3
import ast
import runpod_compat as runpod
import os
from datetime import datetime

from mydotenv import load_env
load_env()

from vast_provider import get_vast_pods

def _get_ports(runtime):
    if not isinstance(runtime, dict):
        return []
    ports = runtime.get('ports') or []
    return ports if isinstance(ports, list) else []

def _get_public_ip_and_ssh_port(pod):
    ports = _get_ports(pod.get('runtime'))
    public_tcp = [
        p for p in ports
        if isinstance(p, dict) and p.get('type') == 'tcp' and p.get('isIpPublic')
    ]
    if public_tcp:
        p = public_tcp[0]
        ip = p.get('ip') or 'N/A'
        port = str(p.get('publicPort') or 'N/A')
        return ip, port
    # Fallback: if top-level 'ports' string indicates 22/tcp, show port 22 without IP
    top_ports = pod.get('ports') or ''
    if isinstance(top_ports, str) and '22/tcp' in top_ports:
        return 'N/A', '22'
    return 'N/A', 'N/A'

def _name_order_key():
    """Sort rows by position in MACHINE_NAME_LIST, not alphabetically.

    That list's order is the proxy port map (index i -> 16000 + i) and so is
    also the order of the ssh config block participants install, and the order
    create_pods fills names in. Sorting by it makes the table line up with all
    three, even when the list isn't alphabetical. Pods whose name isn't in the
    list at all -- e.g. created by hand outside the tooling -- sort last,
    alphabetically.
    """
    try:
        names = ast.literal_eval(os.environ["MACHINE_NAME_LIST"])
    except Exception:
        names = []
    index = {n: i for i, n in enumerate(names)}
    return lambda row: (index.get(row[0], len(index)), row[0])


def _short_name(name):
    """Drop the shared "<prefix>-" so names read as just "alder"."""
    prefix = (os.getenv("MACHINE_NAME_PREFIX") or "gpu") + "-"
    if name.startswith(prefix):
        return name[len(prefix):]
    return name

def _is_billing(status):
    """Only running pods bill by the hour.

    Stopped/exited pods keep a costPerHr in the API response (the rate they'd
    resume at), so summing every row overstates current spend. Their real
    ongoing charge is idle disk storage, which rounds to zero at these sizes.
    """
    return str(status).lower() == "running"


def list_pods():
    # Fetch RunPod pods (best-effort: a RunPod outage must not hide Vast pods)
    pods = []
    failed = False
    api_key = os.getenv("RUNPOD_API_KEY")
    if not api_key:
        print("# Warning: RUNPOD_API_KEY not set; skipping RunPod pods")
        failed = True
    else:
        runpod.api_key = api_key
        try:
            print("Fetching pods...")
            pods = runpod.get_pods() or []
        except Exception as e:
            print(f"# ERROR: failed to fetch RunPod pods: {e}")
            failed = True

    # Fetch Vast pods (best-effort; returns [] with a warning on failure)
    vast_pods = sorted(get_vast_pods(), key=lambda x: x["name"] or "")

    if not pods and not vast_pods:
        print("Could not list RunPod pods (see error above)" if failed else "No pods found")
        return 1 if failed else 0

    try:
        runpod_cost = 0.0
        vast_cost = 0.0
        rows = []  # unified list so we can sort by name across providers

        for pod in pods:
            try:
                public_ip, ssh_port = _get_public_ip_and_ssh_port(pod)
                # desiredStatus is a clean PodStatus enum (RUNNING/EXITED/...);
                # lowercase it to line up with Vast's actual_status casing.
                status = (pod.get('desiredStatus') or 'N/A').lower()
                cost_val = pod.get('costPerHr')
                cost = f"${cost_val}" if cost_val is not None else "$0.00"
                if _is_billing(status):
                    try:
                        runpod_cost += float(cost_val) if cost_val is not None else 0.0
                    except (TypeError, ValueError):
                        pass
                name = _short_name(pod.get('name') or 'N/A')
                gpu = (pod.get('machine') or {}).get('gpuDisplayName') or 'N/A'
                rows.append((name, public_ip, ssh_port, cost, 'runpod', status, gpu))
            except Exception as e:
                print(f"Warning: failed to render a pod entry: {e}")
                continue

        for vp in vast_pods:
            try:
                public_ip = vp["ip"] or "N/A"
                ssh_port = vp["ssh_port"] or "N/A"
                cost_val = vp["cost_per_hr"]
                cost = f"${cost_val:.2f}" if cost_val is not None else "$0.00"
                if cost_val is not None and _is_billing(vp["status"]):
                    vast_cost += float(cost_val)
                name = _short_name(vp["name"] or "N/A")
                rows.append((name, public_ip, ssh_port, cost, 'vast', vp["status"], vp["gpu"]))
            except Exception as e:
                print(f"Warning: failed to render a Vast pod entry: {e}")
                continue

        rows.sort(key=_name_order_key())

        # Column headers, in row order. Each column is sized to the widest of
        # its header and its data, so no space is wasted past the last value.
        headers = ["Name", "IP", "Port", "Cost", "Prov", "Status", "GPU"]
        widths = [len(h) for h in headers]
        for row in rows:
            for i, cell in enumerate(row):
                widths[i] = max(widths[i], len(str(cell)))

        gap = "  "  # 2 spaces between columns
        def fmt(cells):
            return gap.join(str(c).ljust(w) for c, w in zip(cells, widths)).rstrip()

        table_width = sum(widths) + len(gap) * (len(widths) - 1)
        border = "=" * table_width

        print("\n" + border)
        print(fmt(headers))
        print(border)
        for row in rows:
            print(fmt(row))
        print(border)
        idle = sum(1 for row in rows if not _is_billing(row[5]))
        line = (f"Current hourly spend: ${runpod_cost + vast_cost:.2f}/hr  "
                f"(runpod ${runpod_cost:.2f}/hr, vast ${vast_cost:.2f}/hr)")
        if idle:
            line += f"  [{idle} non-running pod(s) excluded]"
        print(line)
        print(border + "\n")

    except Exception as e:
        print(f"Error: {str(e)}")

if __name__ == "__main__":
    import sys
    sys.exit(list_pods() or 0)