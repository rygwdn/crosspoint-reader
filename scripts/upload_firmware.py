#!/usr/bin/env python3
"""
Upload a built firmware.bin to a CrossPoint Reader device's SD card, over
plain WebDAV PUT (port 80) -- the same server the device's own web UI uses,
just the standard verb instead of its WebSocket upload protocol. This does
NOT flash the firmware; it just gets the .bin onto the SD card so you can
flash it from the device itself via Settings -> Update Firmware from SD.

Resilient by design, because this hardware (and this environment) is flaky in
exactly these ways:
  - The device sleeps and drops WiFi periodically -- both the pre-flight
    status check and the PUT itself retry a few times with generous timeouts
    before giving up, rather than failing on the first slow response.
  - In some sandboxed environments, Python's own resolver (socket.getaddrinfo,
    which urllib uses) fails to resolve crosspoint.local's mDNS name even
    though curl and the OS resolver succeed against the exact same host --
    same class of issue as the port-81 raw-socket problem documented in
    ws_upload.py. Every network call here goes through curl instead of
    urllib/socket for exactly this reason.

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
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

DEFAULT_HOST = "crosspoint.local"
DEFAULT_LOCAL_CANDIDATES = [
    REPO_ROOT / ".pio" / "build" / "default" / "firmware.bin",
    REPO_ROOT / ".pio" / "build" / "gh_release" / "firmware.bin",
]
STATUS_CHECK_TIMEOUT = 15.0
# The PUT itself needs much more headroom than the status check: a multi-MB
# firmware image over weak WiFi (RSSI in the -80s is routine on this hardware,
# see WebDAVHandler.cpp's PROPFIND comments) can legitimately take well over a
# minute at the ~100-300KB/s observed on real devices.
PUT_CONNECT_TIMEOUT = 15
PUT_MAX_TIME = 180


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


def curl_get(url: str, timeout: float, connect_timeout: float) -> tuple[bytes, int]:
    """Fetch url via curl rather than urllib -- see the module docstring for why.
    Returns (body, http_status); raises ConnectionError if curl couldn't complete the
    request at all (DNS failure, connection refused, timeout)."""
    result = subprocess.run(
        ["curl", "-s", "-m", str(timeout), "--connect-timeout", str(connect_timeout), "-w", "\n%{http_code}", url],
        capture_output=True,
    )
    if result.returncode != 0:
        raise ConnectionError(f"curl exit {result.returncode}: {result.stderr.decode(errors='replace').strip()}")
    output = result.stdout
    idx = output.rfind(b"\n")
    body, code = (output[:idx], output[idx + 1 :]) if idx != -1 else (b"", output)
    return body, int(code.strip() or 0)


def check_device(host: str, retries: int = 3, delay: float = 3.0) -> dict | None:
    """GET /api/status a few times before giving up -- the device sleeps and
    periodically drops off WiFi, so one failed request doesn't mean it's gone."""
    url = f"http://{host}/api/status"
    for attempt in range(1, retries + 1):
        try:
            body, code = curl_get(url, timeout=STATUS_CHECK_TIMEOUT, connect_timeout=STATUS_CHECK_TIMEOUT)
            if code == 200:
                return json.loads(body.decode())
            print(f"  [{attempt}/{retries}] {host} responded HTTP {code}")
        except ConnectionError as e:
            print(f"  [{attempt}/{retries}] {host} not responding yet ({e})")
        if attempt < retries:
            time.sleep(delay)
    return None


def upload_via_webdav(host: str, port: int, local_path: Path, remote_path: str, retries: int, retry_delay: float) -> bool:
    url = f"http://{host}:{port}{remote_path}"
    for attempt in range(1, retries + 1):
        if attempt > 1:
            print(f"=== Retry {attempt}/{retries} ===")
        result = subprocess.run(
            [
                "curl",
                "--fail",  # non-2xx (e.g. 403 from a protected path, 500) is a failure, not "done"
                "--connect-timeout",
                str(PUT_CONNECT_TIMEOUT),
                "--max-time",
                str(PUT_MAX_TIME),
                "-T",
                str(local_path),
                url,
            ]
        )
        if result.returncode == 0:
            return True
        print(f"  curl exited {result.returncode}")
        if attempt < retries:
            time.sleep(retry_delay)
    return False


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "local_path", nargs="?", default=None, help="Firmware .bin to upload (default: auto-detect a fresh build)"
    )
    parser.add_argument("--host", default=DEFAULT_HOST, help=f"Device host/IP (default: {DEFAULT_HOST})")
    parser.add_argument("--name", default=None, help="Remote filename (default: firmware-<commit>[-dirty].bin)")
    parser.add_argument("--dir", default="/", help="Remote directory on the device (default: /, must already exist)")
    parser.add_argument("--port", type=int, default=80, help="WebDAV/HTTP port (default: 80)")
    parser.add_argument("--retries", type=int, default=3, help="PUT retries on failure (default: 3)")
    parser.add_argument("--retry-delay", type=float, default=3.0, help="Seconds between retries (default: 3)")
    parser.add_argument("--skip-status-check", action="store_true", help="Skip the pre-flight /api/status check")
    args = parser.parse_args()

    local_path = Path(args.local_path) if args.local_path else default_local_path()
    if local_path is None:
        print("error: no firmware.bin found. Build one first (`pio run`), or pass a path explicitly.", file=sys.stderr)
        print(f"  looked in: {', '.join(str(c) for c in DEFAULT_LOCAL_CANDIDATES)}", file=sys.stderr)
        sys.exit(1)
    if not local_path.exists():
        print(f"error: {local_path} does not exist", file=sys.stderr)
        sys.exit(1)

    remote_name = args.name or default_remote_name()
    remote_dir = args.dir.rstrip("/")  # "" for the default "/", so the join below never double-slashes
    remote_path = f"{remote_dir}/{remote_name}"

    print(f"Checking device at {args.host}...")
    status = None if args.skip_status_check else check_device(args.host)
    if status:
        print(f"  reachable: version={status.get('version')} rssi={status.get('rssi')} uptime={status.get('uptime')}")
    else:
        print(f"  {args.host} did not respond to /api/status -- proceeding anyway (may just be asleep/mid-reconnect)")

    print(f"Uploading {local_path} ({local_path.stat().st_size} bytes) to {remote_path} via WebDAV PUT ...")
    ok = upload_via_webdav(args.host, args.port, local_path, remote_path, args.retries, args.retry_delay)
    if not ok:
        print(
            f"\nUpload failed after {args.retries} attempt(s). If {args.host} doesn't resolve, "
            "try again with --host <device-ip>. If --dir isn't '/', make sure that directory "
            "already exists on the device -- WebDAV PUT won't create it.",
            file=sys.stderr,
        )
        sys.exit(1)

    print(
        f"\nDone. {remote_name} is on the device's SD card at {remote_path}.\n"
        "Flash it from the device: Settings -> Update Firmware from SD."
    )


if __name__ == "__main__":
    main()
