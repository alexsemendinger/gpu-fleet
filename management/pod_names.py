#!/usr/bin/env python3
"""Pod-name prefix helpers shared across the create/stop/delete/kill scripts.

Pod names have two forms: the bare machine name ("alder") and the full,
provider-visible name ("gpu-alder" == MACHINE_NAME_PREFIX + "-" + bare).
Historically each entrypoint accepted only one form -- the create_* scripts
wanted bare names (and would cheerfully make "gpu-gpu-alder" if you
passed the full form), while the stop/delete/kill scripts matched on full
names (so a bare "alder" matched nothing). These helpers let every
entrypoint accept EITHER form from the user and normalize internally.

Both helpers are idempotent, so it's safe to run user input through them
regardless of which form was typed.
"""
import os


def _prefix():
    # config.env is loaded by the caller's load_env() before these run;
    # default to "gpu" so they're safe even if the var is unset.
    return os.getenv("MACHINE_NAME_PREFIX") or "gpu"


def to_bare(name):
    """Strip the machine-name prefix if present ("gpu-alder" -> "alder")."""
    name = name.strip()
    pre = _prefix() + "-"
    return name[len(pre):] if name.startswith(pre) else name


def to_full(name):
    """Add the machine-name prefix if absent ("alder" -> "gpu-alder")."""
    name = name.strip()
    pre = _prefix() + "-"
    return name if name.startswith(pre) else pre + name
