# Claude operating notes

This repo runs a fleet of GPU pods for a program: pods on RunPod (and optionally
Vast.ai), reached by participants through an nginx SSH proxy on an always-on box.
You (Claude) are usually invoked on that **proxy box** by an organizer, to provision
pods, diagnose problems, or run routine ops. Default to the shortcut commands,
default to small reversible actions, and never destroy a pod without explicit
approval.

**Clock.** The proxy box runs on UTC, so `date` won't show the organizer's local
time. When they say "at 9am", ask once which timezone they mean, and write it here
(below) so future sessions don't have to ask:

- Organizer timezone: _(fill in)_

---

## Where to look first

| Question | Source |
|---|---|
| What is this, how is it set up? | `README.md` |
| How do I run a session well / avoid costly mistakes? | `OPS_PLAYBOOK.md` |
| A pod needs persistent storage | `NETWORK_VOLUMES.md` + `create_volume_pod` |
| Settings (names, GPU, image, proxy host) | `config.env` (never commit it) |
| API keys to hand out | `keys/*.csv` (gitignored) + `deploy_keys` |
| How scripts talk to RunPod | `management/runpod_compat.py`: a small REST v2 client (`https://api.runpod.io/v2`). Import it as `import runpod_compat as runpod`. **Never use the pip `runpod` SDK** — it speaks RunPod's GraphQL API, which is being retired. |

---

## Shortcut commands

Installed by `install.sh` as symlinks from `bin/` onto the PATH. Each is a thin
wrapper over a script in `management/` or `proxy/`. **Read this reference or the
script instead of running `--help`.**

The standard way to bring up a fleet (see "Provisioning when availability is
tight" below for why `burst_create_pods` is the default):
```bash
burst_create_pods -n 12 --max-price 0.50
# wait ~2–5 min; watch list_pods until every row shows an IP (not N/A)
update_proxy
podcheck
deploy_keys        # only if the pods need API keys
```
Or hand the whole post-create sequence to `management/pod_pipeline.sh <names...>`.

**Never chain a create with `update_proxy` via `&&`.** Pods need minutes after
creation to get a public IP, and a proxy config generated before the IPs settle
silently leaves those pods out.

### Reference

**`list_pods`** — table of every pod on both providers, with total hourly spend
(running pods only). Rows follow `MACHINE_NAME_LIST` order, which is also the proxy
port order and the order of participants' SSH config.

**`fleet_report`** — per-pod uptime and estimated spend so far, from each provider's
real start time. est$ = rate × uptime (approximate).

**`fleet_status`** — one machine-readable line per name: `HAS_IP` / `NO_IP` /
`MISSING`, for scripting.

**`podcheck [names...]`** — SSH + CUDA check through the proxy. No args = every name.
Accepts bare names (`podcheck alder maple`) or single-letter shorthand for the first
name with that initial (`podcheck a m`). `cuda: NO` means a bad host: destroy the pod
and draw another.

**`create_pods`** (→ `management/create_new_pods.py`) — RunPod, one attempt per name,
prompts `(y/N)` (pipe `printf 'y\n' |` to confirm). Selection:
`-n N` (first N names), `-a N` (N more on unused names), or positional names, bare
(`alder`) or prefixed (`gpu-alder`). Overrides: `--gpu-type "NVIDIA A40"` (quote it),
`--gpu-count N`, `--cloud-type COMMUNITY|SECURE`, `--docker-image`,
`--disk-space-in-gb N`, `--volume-space-in-gb N`. Often creates nothing when capacity
is tight — use `burst_create_pods`.

**`burst_create_pods`** — the default creator. Same selection flags as `create_pods`,
plus `--max-price` (hard cap, default 0.50), `--gpu-types` (comma list),
`--timeout` (default 1800s). Retries continuously in parallel across every NVIDIA GPU
type under the cap until each name lands. No prompt; a selection is required. Don't
pipe its output through anything that closes stdout early (`head`).

**`create_vast_pods`** — Vast.ai. Same selection flags. `--gpu-name RTX_3090`
(underscores), `--num-gpus`, `--disk`, `--image`, `--max-price` (default $0.60/hr).

**`create_volume_pod <name>`** — a SECURE pod on a persistent network volume; see
`NETWORK_VOLUMES.md`. Re-running with the same name reuses the same volume.

**`stop_pods`** — stops running pods on both providers. **A stopped pod keeps no
data**; this is only useful to pause billing when the pod may be resumed soon.
`--include` / `--exclude` narrow it.

**`delete_pods`** — deletes already-stopped pods on both providers (never touches
running ones). `--include` / `--exclude` narrow it.

**`nuke_pods`** — **destructive**: stops and destroys running pods on both providers,
no prompts. `--include` / `--exclude`, `--timeout 300` (RunPod side). A full teardown
of a mixed fleet is `nuke_pods` then `delete_pods`.

**`deploy_keys`** (→ `management/copy_api_keys.py`) — see below. `--pod <prefix>-X`
(repeatable) restricts it; `--max-parallel N` (default 30).

**`update_proxy`** — regenerates the nginx config from live pod IPs and reloads nginx.
If the new config fails `nginx -t`, the old one stays in place.

**`ssh_config`** — prints the `~/.ssh/config` block participants install (one `Host`
per name, each pinned to its proxy port).

**`show_key`** — prints the shared private SSH key to hand to participants.

**`keepalive_pod <name>`** — makes sure one named pod is up and CUDA-healthy,
replacing it if not. Safe to run from cron (e.g. a "test your SSH setup" pod before
the first session).

Other scripts, run directly: `management/fill_fleet.sh` (drive every name to
passing, unattended), `management/run_cmd.sh <cmd>` (run on every pod),
`management/gpu-top.sh` (live GPU use across the fleet), `management/tree_relay.sh`
(copy a model cache pod-to-pod), `management/generate_openrouter_keys.py` and
`management/openrouter_spend_report.py` (per-pod OpenRouter keys and their spend).

---

## `deploy_keys`

Reads `keys/{openrouter,openai,anthropic,huggingface}_api_keys.csv` (two columns:
full pod name, key), finds each running pod's IP/port through the provider APIs, and
writes on each pod:

- `POD_ENV_FILE` (default `/root/.env`) — `KEY="value"` lines, atomic, mode 600.
- `~/.bashrc` and `~/.zshrc` — a marker-guarded block that sources it (idempotent).
- `/root/.ipython/profile_default/startup/00-load-env.py` — loads it into
  `os.environ` on every Jupyter/IPython kernel start.

The HF token is written as both `HF_TOKEN` and `HUGGING_FACE_HUB_TOKEN`. Offline pods
and missing CSVs are skipped silently.

- **"My notebook says `KeyError: OPENROUTER_API_KEY`"** → was `deploy_keys` run after
  the pod was created, and was the kernel restarted since? Check the pod has the file:
  `ssh <prefix>-<name> 'cut -c1-20 /root/.env'`.
- **Shared keys (one key for everyone)** → a CSV with one row per name, all the same
  key, saved as `keys/<provider>_api_keys.csv`, then `deploy_keys`. Ask the organizer
  for the key string — never invent one.
- **Per-pod OpenRouter keys** → `management/generate_openrouter_keys.py` mints one
  capped key per name into `keys/openrouter_api_keys.csv` (needs the provisioning key
  in `keys/.openrouter_provisioning_key`).

---

## Provisioning when availability is tight

This is the **normal** case, not the exception.

**What you'll see.** `create_pods -n 12` reports errors like:
- `There are no longer any instances available with the requested specifications.`
- `This machine does not have the resources to deploy your pod.`

Both mean "no capacity for that GPU type right now" — not a bug, not a config
problem. Capacity churns second to second, so the fix is to **retry hard and spread
across GPU types and providers.**

**Why aggressive retrying is safe:**
1. **A failed RunPod create costs nothing and leaves no pod.** There is no
   half-created or billed-but-stuck state.
2. **Community prices are fixed per GPU type.** To stay under $X/hr, restrict which
   types you try. Roughly a dozen NVIDIA types (A4000, A5000, 3090, 4090, A6000, A40,
   …) are usually under $0.50/hr on community cloud, and `burst_create_pods` reads the
   live price list itself. Don't pin a single type when supply is short. Skip AMD and
   MIG slices; the images are CUDA-only.
3. **Vast.ai is a second source**, but its cheap supply is often thin, and its query
   syntax is unreliable for filtering; filter offers in Python instead. Don't burn
   time forcing Vast for one marginal pod if RunPod is delivering.

Run `burst_create_pods` in the background and watch it: it prints `[OK] <pod> <- <gpu>`
as each lands, with a progress line every 15s. Expect minutes and hundreds of silent
retries per pod when capacity is scarce — let it grind.

---

## Hard rules

- **Never** stop, delete or destroy pods without the organizer naming them. Pods can
  hold participants' active work, and a stopped pod keeps nothing.
- **Never** commit API keys. `config.env` and `keys/` are gitignored; keep it that way.
- **Never** hardcode a key in any script or doc.
- **Never** leave a destructive command waiting on an approval prompt in an unattended
  window: a pending prompt freezes the whole session, including anything scheduled.
- Before any command that could create or destroy pods in bulk, know how you would
  undo it.
