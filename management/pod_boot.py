"""The optional boot hook (POD_SETUP_CMD in config.env), for both providers."""
import base64
import os


def setup_cmd():
    return (os.getenv("POD_SETUP_CMD") or "").strip()


def _encoded():
    # base64 so the snippet can contain any characters (quotes, $, ;) without
    # surviving several layers of shell and API quoting.
    return base64.b64encode(setup_cmd().encode()).decode()


def runpod_start_args():
    """None (boot the image as-is) unless POD_SETUP_CMD is set.

    When it is set, the snippet runs in bash first and then the command chains
    into the image's /start.sh, which starts sshd with the injected
    PUBLIC_KEY. Replacing the image CMD without that chain would leave the pod
    unreachable. A failing snippet doesn't stop the boot (`;`, not `&&`).
    """
    if not setup_cmd():
        return None
    return f"bash -c 'echo {_encoded()} | base64 -d | bash; exec /start.sh'"


def vast_onstart_block():
    """Lines for Vast's onstart script ("" without a hook); failures are ignored."""
    if not setup_cmd():
        return ""
    return f"echo '[onstart] running POD_SETUP_CMD'\necho {_encoded()} | base64 -d | bash || true\n"
