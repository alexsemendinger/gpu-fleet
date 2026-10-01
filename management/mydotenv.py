#!/usr/bin/env python3
"""Read config.env into os.environ, the way bash's `source config.env` would.

config.env is also sourced by the shell tools, so both readers must agree:
values follow shell quoting, `#` starts a comment only at the start of a word
(outside quotes), and bash arrays may be written on one line, `NAMES=("a"
"b")`, or across several. Arrays are stored as the str() of a Python list,
which callers read back with ast.literal_eval. `$VAR` / `${VAR}` are
expanded as bash would (except inside single quotes); command substitution is not.
"""
import os
import shlex
import sys


def _scan(text, stop_at_paren=False):
    """Return (text without its comment, index of the first unquoted ")" or None).

    Follows bash: quotes and backslashes protect characters, and `#` begins a
    comment only at the start of a word.
    """
    quote = None
    i = 0
    while i < len(text):
        c = text[i]
        if quote:
            if c == "\\" and quote == '"':
                i += 2
                continue
            if c == quote:
                quote = None
        elif c == "\\":
            i += 2
            continue
        elif c in "'\"":
            quote = c
        elif c == "#" and (i == 0 or text[i - 1].isspace() or text[i - 1] == "("):
            return text[:i], None
        elif c == ")" and stop_at_paren:
            return text, i
        i += 1
    return text, None


def _words(text):
    try:
        return shlex.split(text, posix=True)
    except ValueError:  # unbalanced quote: fall back to the raw text
        return [text.strip().strip('"').strip("'")]


def load_dotenv(env_path):
    """Load KEY=value lines and KEY=( ... ) arrays from env_path into os.environ."""
    if not os.path.exists(env_path):
        return False
    with open(env_path) as f:
        lines = f.read().splitlines()

    i = 0
    while i < len(lines):
        line = lines[i].strip()
        i += 1
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, rest = line.split("=", 1)
        key = key.strip()
        if key.startswith("export "):
            key = key[len("export "):].strip()
        if not key.isidentifier():
            continue

        if rest.startswith("("):
            # Bash array: collect comment-stripped text up to the closing ")",
            # possibly across several lines.
            body = ""
            chunk = rest[1:]
            while True:
                text, close = _scan(chunk, stop_at_paren=True)
                if close is not None:
                    body += " " + text[:close]
                    break
                body += " " + text
                if i >= len(lines):
                    break
                chunk = lines[i]
                i += 1
            os.environ[key] = str(_words(body))
            continue

        raw = _scan(rest)[0]
        value = " ".join(_words(raw))
        if "$" in raw and not raw.strip().startswith("'"):
            value = os.path.expandvars(value)   # e.g. "$HOME/.ssh/key", as bash would
        os.environ[key] = value
    return True


def load_env():
    """Load config.env from the repo root, or exit with a clear message."""
    script_dir = os.path.dirname(os.path.abspath(__file__))
    config_path = os.path.normpath(os.path.join(script_dir, "..", "config.env"))
    if not load_dotenv(config_path):
        sys.exit(f"config.env not found at {config_path} — copy config.env.example "
                 "to config.env and fill it in (see README).")
