#!/usr/bin/env python3
"""
Upload a built firmware.bin to a CrossPoint Reader device's SD card, over the
device's binary WebSocket file-upload protocol (port 81) -- the same one its
own "Files" web UI uses. This does NOT flash the firmware; it just gets the
.bin onto the SD card so you can flash it from the device itself via
Settings -> Update Firmware from SD.

Resilient by design, because this hardware is flaky in exactly these ways:
  - crosspoint.local (mDNS) routinely fails to resolve even when the device
    is reachable -- falls back to trying the hostname anyway (some
    resolvers/networks do handle it) and gives a clear next step if it can't.
  - The device sleeps and drops WiFi periodically -- the pre-flight status
    check retries a few times before giving up, and the upload itself
    (ws_upload.upload) retries on a mid-transfer broken pipe, which is normal
    on weak signal, not a bug.

Usage:
  python3 scripts/upload_firmware.py                      # crosspoint.local, .pio/build/default/firmware.bin
  python3 scripts/upload_firmware.py --host 192.168.0.143
  python3 scripts/upload_firmware.py path/to/other.bin --name my-test.bin
"""
import argparse
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
WS_UPLOAD = REPO_ROOT / ".claude" / "skills" / "device-network" / "scripts" / "ws_upload.py"

DEFAULT_HOST = "crosspoint.local"
DEFAULT_LOCAL_CANDIDATES = [
    REPO_ROOT / ".pio" / "build" / "default" / "firmware.bin",
    REPO_ROOT / ".pio" / "build" / "gh_release" / "firmware.bin",
]


def git(*args):
    try:
        return subprocess.check_output(["git", *args], cwd=REPO_ROOT, stderr=subprocess.DEVNULL).decode().strip()
    except Exception:
        return None


def default_local_path() -> Path | None:
    for candidate in DEFAULT_LOCAL_CANDIDATES:
        if candidate.exists():
            return candidate
    return None


def default_remote_name() -> str:
    commit = git("rev-parse", "--short=8", "HEAD") or "unknown"
    dirty = bool(git("status", "--porcelain"))
    suffix = "-dirty" if dirty else ""
    return f"firmware-{commit}{suffix}.bin"


def check_device(host: str, retries: int = 3, delay: float = 3.0) -> dict | None:
    """GET /api/status a few times before giving up -- the device sleeps and
    periodically drops off WiFi, so one failed request doesn't mean it's gone."""
    url = f"http://{host}/api/status"
    for attempt in range(1, retries + 1):
        try:
            with urllib.request.urlopen(url, timeout=8) as resp:
                return json.loads(resp.read().decode())
        except (urllib.error.URLError, OSError, TimeoutError) as e:
            print(f"  [{attempt}/{retries}] {host} not responding yet ({e})")
            if attempt < retries:
                time.sleep(delay)
    return None


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "local_path", nargs="?", default=None, help="Firmware .bin to upload (default: auto-detect a fresh build)"
    )
    parser.add_argument("--host", default=DEFAULT_HOST, help=f"Device host/IP (default: {DEFAULT_HOST})")
    parser.add_argument("--name", default=None, help="Remote filename (default: firmware-<commit>[-dirty].bin)")
    parser.add_argument("--dir", default="/", help="Remote directory (default: /)")
    parser.add_argument("--port", type=int, default=81, help="WebSocket upload port (default: 81)")
    parser.add_argument("--retries", type=int, default=3, help="Upload retries on a dropped connection (default: 3)")
    parser.add_argument("--skip-status-check", action="store_true", help="Skip the pre-flight /api/status check")
    args = parser.parse_args()

    if not WS_UPLOAD.exists():
        print(f"error: expected {WS_UPLOAD} (WebSocket upload implementation) but it's missing", file=sys.stderr)
        sys.exit(1)

    local_path = Path(args.local_path) if args.local_path else default_local_path()
    if local_path is None:
        print("error: no firmware.bin found. Build one first (`pio run`), or pass a path explicitly.", file=sys.stderr)
        print(f"  looked in: {', '.join(str(c) for c in DEFAULT_LOCAL_CANDIDATES)}", file=sys.stderr)
        sys.exit(1)
    if not local_path.exists():
        print(f"error: {local_path} does not exist", file=sys.stderr)
        sys.exit(1)

    remote_name = args.name or default_remote_name()

    print(f"Checking device at {args.host}...")
    if args.skip_status_check:
        status = None
    else:
        status = check_device(args.host)
    if status:
        print(f"  reachable: version={status.get('version')} rssi={status.get('rssi')} uptime={status.get('uptime')}")
    else:
        print(f"  {args.host} did not respond to /api/status -- proceeding anyway (may just be asleep/mid-reconnect)")

    print(f"Uploading {local_path} ({local_path.stat().st_size} bytes) as {args.dir}{remote_name} ...")
    sys.path.insert(0, str(WS_UPLOAD.parent))
    import ws_upload  # noqa: E402

    ok = ws_upload.upload(args.host, args.port, str(local_path), remote_name, args.dir, retries=args.retries)
    if not ok:
        print(
            f"\nUpload failed after {args.retries} attempt(s). If {args.host} doesn't resolve, "
            "try again with --host <device-ip>.",
            file=sys.stderr,
        )
        sys.exit(1)

    print(
        f"\nDone. {remote_name} is on the device's SD card at {args.dir}{remote_name}.\n"
        "Flash it from the device: Settings -> Update Firmware from SD."
    )


if __name__ == "__main__":
    main()
