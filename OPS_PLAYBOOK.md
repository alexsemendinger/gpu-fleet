# GPU ops playbook

The "do this" version of running compute for a program: the practices that produced
good outcomes, and the handful of hard rules where getting it wrong costs real money or
data. It lives in the repo so it survives operator changes.

Companion docs: `README.md` (setup and daily flow), `CLAUDE.md` (command reference for
Claude and humans alike), `NETWORK_VOLUMES.md` (persistent storage).

---

## 1. Provisioning a pod

**Retry, and retry across options.** A single create attempt routinely fails for lack
of capacity — that is normal, not a bug. `create_pods` retries for you, rotating through
every GPU type under the price cap until each pod lands. A failed create costs nothing
and leaves no pod.

**Run parallel attempts when someone is waiting.** Two pods hunting the same spec land
far faster than one. Keep the first that becomes *usable*, terminate the other. It is
common for one twin to be ready in under three minutes while the other never comes up.

**Try datacenters near your participants first.** Pin `data_center_id` explicitly when
you care where it lands.

**Prefer COMMUNITY, fall back to SECURE.** Community is ~20–35% cheaper for the same card.
Volume-backed pods are SECURE-only.

**Treat availability flags as hints.** "Available" frequently fails to create, and
"unavailable" sometimes succeeds. Attempt the create; believe the result.

**Size the container disk to the job.** A 27B model in bf16 is ~54 GB, a 35B ~70 GB;
add room for datasets, checkpoints and a HuggingFace cache. 200–500 GB is a sane default
for training work.

## 2. Declaring a pod ready

**Verify before telling anyone it is up.** The check that counts is, over SSH:
`nvidia-smi`, `torch.cuda.is_available()`, `torch.cuda.device_count()` (for multi-GPU),
`df -h` on both `/` and `/workspace`, and `check_pods <name>`. A pod whose CUDA check fails
is on a bad host: destroy it and draw another; it will not fix itself.

**Expect SSH to lag the IP by a few minutes.** The image boots (and runs `POD_SETUP_CMD`,
if set) before `/start.sh` starts sshd. "Connection refused" right after the IP appears is
normal; treat it as a problem only after ~10 minutes.

**Get pods up before people arrive.** A cold pod takes 5–20 minutes from create to usable.
Build the fleet ahead of the session, then `ready_pods`, so participants walk in to
working machines.

**Announce readiness proactively.** Tell the user the moment a pod answers. Writing to a
log file that nobody reads is the same as not checking.

**Start anyone's billing clock at confirmed-ready**, not at the provider's `createdAt`.
Boot time and rebuild churn are the operator's cost, not the group's.

**Bring a whole fleet up as independent pipelines, never as a batch.**
`ready_pods` handles each pod separately and in parallel:
IP → `update_proxy` → `check_pods` → `deploy_keys`, so each pod advances the moment it is
individually ready and one slow pod never gates the rest. It prints `[<name>] READY` or
`[<name>] FAILED <stage>` per pod, which is also the honest way to report progress.

**Seeding a large model cache onto many pods: relay, don't fan out from HuggingFace.**
A shared HF token rate-limits (~1000 requests / 5 min), so parallel `snapshot_download`s
across a fleet get 429s. Download once onto one pod, then
`RELAY_DIRS="hub/models--org--repo ..." management/tree_relay.sh <seed-pod> <target>...`
copies pod-to-pod over a tar pipe in a tree: every verified target becomes a source for
the next round, so N pods take ~log2(N) rounds. Expect 60–100 MB/s per hop. For pods far
from the proxy a direct HF download can be faster — relay is for dodging rate limits, not
for raw speed. Verify by comparing per-file byte sizes against a known-good pod, not by
`du` (interrupted downloads leave truncated shards that look complete).

## 3. Network volumes

**Decide storage before creating the pod.** A volume attaches only at creation, and only
in its own datacenter. Adding one later means destroying and rebuilding the pod.

**Check that the GPU exists in the volume's region before promising it.** Some GPU models
are simply not deployed in some datacenters, regardless of stock. `NETWORK_VOLUMES.md` has
the catalog query that intersects volume support with GPU availability.

**Pin volumes by `--volume-id`, always.** `create_volume_pod` derives the volume *name*
from the pod name, so it records `<prefix>-<pod>-vol` even when you attached a shared
volume. Patch `management/network_volumes.json` after every shared-volume create: set
`network_volume_name`, `network_volume_id`, `volume_size_gb`, and a `recreate_cmd` that
carries `--volume-id`. A name-based recreate silently creates a NEW EMPTY volume and
hands the group a blank disk.

**Share one volume across pods freely.** Concurrent read-write from several pods works.
Tell groups to use per-person subdirectories for anything they write; simultaneous writes
to one path corrupt silently with no error.

**Keep private keys off volumes.** The volume filesystem reports mode 0666 whatever you
chmod, and SSH refuses such keys. Copy keys to local disk and `chmod 600` before use.

**Read volume size from the API, not from inside a pod.** `df` on a volume mount shows
the whole storage cluster (hundreds of TB), never your quota.

## 4. Changing a pod in place

**Resize disk through the API** — `PATCH https://api.runpod.io/v2/pods/{id}` with
`{"disk": N}`. The older v1 API did this live on a running pod with no restart; the v2
call has not been tested for that yet, so try it on a throwaway pod before relying on it
for someone's running work.

**Expand volumes in place** — `PATCH https://api.runpod.io/v2/network-volumes/{id}` with
`{"size": N}`. Increases only. Confirm from the API; the pod cannot show you the quota.

**Before proposing a rebuild, state its price.** A swap costs (a) the risk of not getting
the GPU back — creates fail constantly — and (b) the boot clock restarting, 5–10 minutes
of the user's window. Say both, then let the user choose. Never describe a rebuild as free.

## 5. Stopping, deleting, and teardown

**Stopping a pod keeps no data, on either provider.** A stopped pod's container disk is
discarded; only network volumes survive. Tell participants to push work to GitHub or
HuggingFace (or use a volume) before a pod is stopped or destroyed, and never offer
"resume" as a way to recover files.

**Schedule every pod's end at creation time.** Use `at` (system-level, survives the
session) to run `destroy_pods --yes <name>` when the group's time is up. `--yes` matters:
without it the command waits for a confirmation that never comes.

**Keep exactly one job per pod.** When a user changes an end time, `atrm` the old job
before adding the new one, then list jobs and confirm the target. Check the clock first.

**Destroy pods when a group is done; don't stop them.** The only thing a stopped pod
buys is the chance that a resume returns the same machine, and that fails far more often
than it works (roughly one in four succeeded in practice: "not enough free GPUs on the
host machine"). Stopped pods also keep their names reserved, and enough of them exhausts
`MACHINE_NAME_LIST`. That's why there is no stop command here. `destroy_pods` also removes
pods that got stopped some other way (from the RunPod console, or by the provider when
credit runs out).

**End-of-program teardown, in order:**
1. `destroy_pods --all` (running and stopped pods, both providers)
2. Delete every network volume — `DELETE https://api.runpod.io/v2/network-volumes/{id}`
3. Verify all three are empty: `list_pods`, the volumes endpoint, and `atq`
4. Confirm no scheduled jobs (`at`, cron) remain that could recreate anything

**Volumes are the bill that outlives the program.** Pods stop costing when destroyed;
volumes charge ~$0.07/GB/month until explicitly deleted (2 TB ≈ $4.80/day). Deleting
volumes is irreversible and destroys work — confirm the groups are finished, then delete.

## 6. Diagnosing a pod that "disconnects"

**Separate the pod from the control plane.** A pod the API shows as running but with no
IP or runtime means RunPod has lost track of the container, not necessarily that it died.
SSH to the last known IP:port directly — it often answers.

**Know the knock-on effect:** the nginx proxy is generated from the API's IP data, so a
pod the API cannot see gets dropped from the proxy on the next `update_proxy`, and
`ssh <prefix>-<name>` starts refusing while the pod is fine. Give the user the direct
endpoint as an immediate workaround.

**Check host load.** `uptime` inside the pod shows the *host's* load. Normal is single
digits; ~40 means the machine is oversubscribed and everything on it will be slow and
flaky. That is a rebuild-elsewhere situation.

**Confirm before alarming.** A single missing-IP poll is usually a blip that clears within
a minute. Require two consecutive failures plus a failed SSH probe before calling a pod
broken.

**Check where a replacement actually landed.** Rebuilding can put you on the same physical
host (same public IP) — compare IPs and host uptime, or the rebuild fixed nothing.

**Bad hosts break every pod on them.** If several pods on the same IP fail CUDA the same
way, it is the machine. Switching GPU type is the reliable way to draw a different host.

## 7. Serving a model for a group (vLLM)

**Install into a dedicated venv**, e.g. `/root/vllm-venv`. Installing vLLM into the
image's main Python environment upgrades `numpy`/`huggingface-hub` and can break other
libraries participants depend on (`transformer_lens` is a common casualty).

**Match the tool-call parser to what the model emits.** Read a raw completion first:
Hermes-style JSON inside `<tool_call>` needs `--tool-call-parser hermes`; Qwen XML
(`<function=name><parameter=x>`) needs `qwen3_coder`. Get the valid names from
`ToolParserManager.list_registered()` — the module filenames are not the registered names.

**Verify tool calls end to end from the machine that will use them**, not from the pod.
`curl /v1/models` proves reachability; only a real request with a `tools` payload proves
`finish_reason: tool_calls` and structured arguments. Silent tool-call failure scores an
eval as 0% with no error.

**Prefer a single GPU when the model fits.** Tensor parallelism pulls in flashinfer paths
that fail on some cards. If flashinfer misbehaves, keep it installed and set
`VLLM_USE_FLASHINFER_SAMPLER=0` — uninstalling it breaks vLLM's sampler import instead.

**Set `--max-num-seqs` below the model's cache-block count** for hybrid Mamba/MoE models;
the default 1024 aborts CUDA graph capture with an explicit error naming the limit.

## 8. Connecting a pod to an outside machine

**Dial outward from the pod.** Run the reverse tunnel from the GPU pod to the external
box (`ssh -N -R <port>:localhost:<port>`), so the outside machine holds no credentials
for the fleet. Restrict the key on the far side:
`restrict,port-forwarding,permitlisten="8000"`.

**Give the tunnel a keepalive** (`ServerAliveInterval=30`) and a watchdog that restarts it,
since a dropped tunnel kills whatever is running through it.

## 9. Money

**Set a per-person or per-group budget up front** and flag groups as they approach it.
Small overruns near the end are acceptable; surprises are not.

**Count API keys as spend.** OpenRouter often rivals compute for API-heavy groups. Read it
with `management/openrouter_spend_report.py`. Raising a key's limit does not spend money,
but it removes the thing that was capping the group.

**Treat a quoted account balance as decaying.** Subtract observed burn (`pod_costs` rate
× elapsed) before repeating a figure, or check it fresh. Provider accounts are usually
prepaid: set up auto-top-up where it exists, because an empty balance stops every pod.

**Watch the top tier.** Multi-GPU H100/H200/B200 boxes dominate spend; cheap cards are
rounding errors. The single biggest saving is stopping expensive pods when idle — don't
keep top-tier GPUs up "in reserve"; spin them up on demand.

## 10. Writing scripts for remote pods

**Write the script to a file and `scp` it**, then run it. Nested quoting inside
`ssh "..."` with heredocs silently mangles commands — a launch command can fail to run
while a poller happily watches a server that never started.

**Use patterns that cannot match themselves.** `pkill -f 'vllm serve'` kills the shell
running it; use `pgrep -af '[v]llm serve'` and kill by PID.

**Make a monitor's filter cover failure, not just success.** A watcher that greps only for
the success string stays silent through a crash, and silence reads as "still working".

## 11. Security

**Make a new shared SSH key for every program**, and never reuse it anywhere else (not on
GitHub, not on other servers). Every pod trusts it and every participant holds it, so it
is only as safe as the least careful holder — and a compromised provider host can read it
off any pod. Rotate it between programs.

**Prefer RunPod over Vast.ai community hosts** when the work is sensitive. Community hosts
are third-party machines, and a host-level compromise exposes everything on the pod.

**Keep keys out of git.** `config.env` and `keys/` are gitignored; keep it that way.
