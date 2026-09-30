"""Container start command for RunPod pods, built from POD_SETUP_CMD."""
import os


def runpod_start_args():
    """None (boot the image as-is) unless POD_SETUP_CMD is set in config.env.

    When it is set, the snippet runs first and then chains into the image's
    /start.sh, which starts sshd with the injected PUBLIC_KEY. Replacing the
    image CMD without that chain would leave the pod unreachable. A failing
    snippet doesn't stop the boot (`;`, not `&&`).
    """
    setup = (os.getenv("POD_SETUP_CMD") or "").strip()
    if not setup:
        return None
    if "'" in setup:
        raise SystemExit("POD_SETUP_CMD must not contain single quotes")
    return f"bash -c '{setup}; exec /start.sh'"
