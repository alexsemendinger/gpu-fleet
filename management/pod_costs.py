#!/usr/bin/env python3
"""Per-pod uptime + estimated spend across RunPod and Vast.

Reads each pod's real provider-side start time (RunPod `createdAt`, Vast
`start_date`), so uptime is correct even across separate operator sessions.
Estimated spend = hourly rate x uptime; it's an approximation that assumes the
pod ran continuously at its current rate since creation.
"""
import ast
import os
from datetime import datetime, timezone

from mydotenv import load_env
load_env()
import runpod_compat as runpod
from pod_names import to_bare
runpod.api_key = os.getenv("RUNPOD_API_KEY")

NOW = datetime.now(timezone.utc)


def dur(sec):
    if sec is None:
        return "?"
    h = int(sec // 3600); m = int((sec % 3600) // 60)
    return f"{h}h{m:02d}m"


rows = []  # name, prov, gpu, rate, start, uptime_s, spend

# --- RunPod: one REST v2 call carries status, cost, createdAt, GPU type ---
fetch_failed = False
try:
    rp_pods = runpod.get_pods() or []
except Exception as e:  # noqa: BLE001
    print(f"# ERROR: runpod fetch failed: {e}")
    rp_pods = []
    fetch_failed = True
for p in rp_pods:
    if p.get("desiredStatus") != "RUNNING":
        continue
    rate = p.get("costPerHr") or 0
    start = runpod.parse_timestamp(p.get("createdAt"))
    up = (NOW - start).total_seconds() if start else None
    disp = (p.get("machine") or {}).get("gpuDisplayName") or "?"
    gpu = f"{p.get('gpuCount') or 1}x {disp}"
    rows.append([to_bare(p["name"]), "runpod", gpu, rate, start, up,
                 rate * up / 3600 if up else None])

# --- Vast (raw instances, for start_date/dph_total), only if configured ---
try:
    if not os.getenv("VASTAI_API_KEY"):
        raise LookupError  # Vast not in use
    from vast_provider import get_vast_client
    c = get_vast_client()
    insts = c.show_instances()
    if isinstance(insts, dict):
        insts = insts.get("instances", [])
    for i in insts:
        label = to_bare(i.get("label") or i.get("name") or str(i.get("id")))
        rate = i.get("dph_total") or 0
        sd = i.get("start_date")
        start = datetime.fromtimestamp(sd, timezone.utc) if sd else None
        up = (NOW - start).total_seconds() if start else None
        gpu = f"{i.get('num_gpus', '?')}x {i.get('gpu_name', '?')}"
        rows.append([label, "vast", gpu, rate, start, up,
                     rate * up / 3600 if up else None])
except LookupError:
    pass
except Exception as e:  # noqa: BLE001
    from vast_provider import _redact
    print(f"# vast fetch failed: {_redact(e)}")

# Order by position in MACHINE_NAME_LIST (== proxy port map == the ssh config
# participants install), so this table reads the same top-down order as
# list_pods and the fill order. Names outside the list sort last.
try:
    _order = {n: i for i, n in enumerate(ast.literal_eval(os.environ["MACHINE_NAME_LIST"]))}
except Exception:  # noqa: BLE001
    _order = {}
rows.sort(key=lambda r: (_order.get(r[0], len(_order)), r[0]))
print(f"Pod costs @ {NOW:%Y-%m-%d %H:%M} UTC   (est$ = rate x uptime, approximate)")
W = max([len(r[0]) for r in rows] + [11])   # name column fits the longest name
print(f"{'pod':{W}} {'prov':6} {'gpu':30} {'$/hr':>6} {'started UTC':12} {'uptime':>8} {'est$':>8}")
print("-" * (W + 75))
tot_rate = tot_spend = 0.0
for name, prov, gpu, rate, start, up, spend in rows:
    s = f"{start:%m-%d %H:%M}" if start else "?"
    print(f"{name:{W}} {prov:6} {gpu[:30]:30} {rate:6.2f} {s:12} {dur(up):>8} "
          f"{('$'+format(spend,'.2f')) if spend is not None else '?':>8}")
    tot_rate += rate
    tot_spend += spend or 0
print("-" * (W + 75))
print(f"{'TOTAL':{W}} {'':6} {'':30} {tot_rate:6.2f} {'':12} {'':>8} {'$'+format(tot_spend,'.2f'):>8}")
if fetch_failed:
    raise SystemExit(1)
