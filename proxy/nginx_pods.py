#!/usr/bin/env python3
import os
import sys
import ast

from mydotenv import load_env
load_env()

from vast_provider import get_vast_pods

# runpod_compat (REST v2 client, legacy-GraphQL-shaped results) lives in
# management/; proxy/ keeps its own mydotenv/vast_provider copies, which win
# on name lookup because the script dir leads sys.path — append, don't insert.
sys.path.append(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "..", "management"))
import runpod_compat


def get_pods(api_key):
    """All RunPod pods, in the legacy shape this script was written against."""
    runpod_compat.api_key = api_key
    return runpod_compat.get_pods()

def list_pods(verbose=False):
    # Fetch RunPod pods (best-effort: a RunPod outage must not drop Vast hosts
    # from the generated proxy config)
    api_key = os.getenv("RUNPOD_API_KEY")
    pods = []
    if not api_key:
        print("# Warning: RUNPOD_API_KEY not set; skipping RunPod pods")
    else:
        try:
            if verbose:
                print("# Fetching pods...")
            pods = get_pods(api_key) or []
        except Exception as e:
            print(f"# Warning: failed to fetch RunPod pods: {e}")
            pods = []

    # Fetch Vast pods (best-effort; [] with a warning on failure)
    vast_pods = get_vast_pods()

    if not pods and not vast_pods:
        if verbose:
            print("# No pods found")
        return

    try:
        # Sort pods by name
        pods = sorted(pods, key=lambda x: x.get('name', ''))

        # Generate Nginx configuration first
        print("# Nginx Configuration")
        print("# -----------------")
        print()
        print("log_format ssh '$remote_addr [$time_local] $protocol $status $bytes_sent $bytes_received $session_time \"$upstream_addr\"';")
        print("access_log /var/log/nginx/ssh_access.log ssh;")
        print("error_log /var/log/nginx/ssh_error.log;")
        print()

        # NATO phonetic alphabet for pod names
        machine_name_prefix: str = os.getenv("MACHINE_NAME_PREFIX")
        machine_name_list: list[str] = ast.literal_eval(os.getenv("MACHINE_NAME_LIST"))
        proxy_starting_port: int = int(os.getenv("SSH_PROXY_STARTING_PORT"))
        # print(f"{machine_name_list=}", len(machine_name_list))
        # Map for storing found pods
        found_pods = {}

        # Find pods that match the NATO naming pattern
        for pod in pods:
            pod_name = pod.get('name', '')
            for i, machine_name in enumerate(machine_name_list):
                pod_name_to_check = f"{machine_name_prefix}-{machine_name}"
                if pod_name == pod_name_to_check:
                    try:
                        # Get runtime ports
                        runtime = pod.get('runtime', {})
                        ports = runtime.get('ports', [])

                        # Find SSH port and IP
                        ssh_port = None
                        public_ip = None

                        for port in ports:
                            if port.get('type') == 'tcp' and port.get('isIpPublic'):
                                ssh_port = str(port.get('publicPort'))
                                public_ip = port.get('ip')
                                break

                        if public_ip and ssh_port:
                            found_pods[machine_name] = {
                                "ip": public_ip,
                                "port": ssh_port,
                                "listen_port": proxy_starting_port + i  # 12000 + index for consistent port numbering
                            }
                    except Exception as e:
                        if verbose:
                            print(f"# Error processing pod {pod_name}: {e}")

        # Merge Vast.ai instances into the SAME machine_name -> listen_port
        # mapping. The listen_port is derived purely from the machine name's
        # index, so a Vast-backed host gets the exact port participants
        # already expect -- they never know which provider backs it.
        # RunPod takes precedence on a name collision (warn and keep RunPod).
        for vp in vast_pods:
            vp_name = vp.get("name") or ""
            for i, machine_name in enumerate(machine_name_list):
                if vp_name != f"{machine_name_prefix}-{machine_name}":
                    continue
                if not (vp.get("ip") and vp.get("ssh_port")):
                    if verbose:
                        print(f"# Vast {vp_name}: no usable IP/port yet "
                              f"(status={vp.get('status')}); skipping")
                    continue
                if machine_name in found_pods:
                    print(f"# Warning: {vp_name} exists on BOTH RunPod and "
                          f"Vast; keeping RunPod, ignoring Vast id={vp.get('id')}")
                    continue
                found_pods[machine_name] = {
                    "ip": vp["ip"],
                    "port": vp["ssh_port"],
                    "listen_port": proxy_starting_port + i,
                }

        # Generate Nginx configuration for found pods
        for machine_name in sorted(found_pods.keys(), key=lambda x: machine_name_list.index(x)):
            data = found_pods[machine_name]
            print(f"upstream {machine_name} {{ server {data['ip']}:{data['port']}; }}")
            print(f"server {{ listen {data['listen_port']}; proxy_pass {machine_name}; }}")
            print()

        # Print table if verbose mode (after nginx config)
        if verbose:
            print("\n" + "="*140)
            print(f"{'IP':<16} {'SSH Port':<10} {'Cost/hr':<10} {'Started (UTC)':<28} {'Name':<15} {'Status':<15} {'GPUs':<6} {'GPU Type':<20} {'Image':<30}")
            print("="*140)

            for pod in pods:
                try:
                    # Get SSH port and IP
                    runtime = pod.get('runtime', {})
                    ports = runtime.get('ports', [])

                    ssh_port = 'N/A'
                    public_ip = 'N/A'

                    for port in ports:
                        if port.get('type') == 'tcp' and port.get('isIpPublic'):
                            ssh_port = str(port.get('publicPort', 'N/A'))
                            public_ip = port.get('ip', 'N/A')
                            break

                    # Started (or created, while still booting), to the minute
                    ts = runpod_compat.parse_timestamp(
                        pod.get('startedAt') or pod.get('createdAt'))
                    status_time = ts.strftime('%m-%d %H:%M UTC') if ts else 'N/A'

                    status = pod.get('desiredStatus', 'N/A')

                    # Format cost
                    cost = f"${pod.get('costPerHr', 0)}"

                    # Get GPU count
                    gpu_count = pod.get('gpuCount', 0)
                    gpu_count_str = f"{gpu_count}x" if gpu_count > 0 else "0"

                    # Get GPU name
                    gpu_name = 'N/A'
                    if pod.get('machine'):
                        gpu_name = pod['machine'].get('gpuDisplayName', 'N/A')

                    # Get image name (truncate if too long)
                    image_name = pod.get('imageName', 'N/A')
                    if len(image_name) > 29:
                        image_name = image_name[:26] + "..."

                    print(f"{public_ip:<16} {ssh_port:<10} {cost:<10} {status_time:<28} {pod.get('name', 'N/A'):<15} {status:<15} {gpu_count_str:<6} {gpu_name:<20} {image_name:<30}")
                except Exception as e:
                    print(f"# Error processing pod: {e}")
                    print(pod)

            # Vast.ai rows (appended to the same table)
            for vp in sorted(vast_pods, key=lambda x: x["name"] or ""):
                try:
                    public_ip = vp["ip"] or "N/A"
                    ssh_port = vp["ssh_port"] or "N/A"
                    cost = f"${vp['cost_per_hr']}" if vp["cost_per_hr"] is not None else "$0"
                    gpu_count_str = f"{vp['num_gpus']}x" if vp["num_gpus"] else "0"
                    image_name = vp["image"]
                    if len(image_name) > 29:
                        image_name = image_name[:26] + "..."
                    print(f"{public_ip:<16} {ssh_port:<10} {cost:<10} {'(vast)':<28} {(vp['name'] or 'N/A'):<15} {vp['status']:<15} {gpu_count_str:<6} {vp['gpu']:<20} {image_name:<30}")
                except Exception as e:
                    print(f"# Error processing Vast instance: {e}")

            print("="*140 + "\n")

    except Exception as e:
        print(f"# Error: {str(e)}")

if __name__ == "__main__":
    # Check for -v flag
    verbose = '-v' in sys.argv
    list_pods(verbose=verbose)
