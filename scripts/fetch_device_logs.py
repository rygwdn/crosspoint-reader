#!/usr/bin/env python3
"""
Pull crash_report.txt and the debug.log generations off a CrossPoint Reader
device, over plain HTTP GET (the same JSON/file API the device's own web UI
uses). Resilient to missing files by design: crash_report.txt only exists
after a panic reset, and there are only as many debug.log.N generations as
the device has rebooted through (see DiskLogger::MAX_LOG_GENERATIONS) -- a
404 for any one of these is an expected, non-fatal outcome, not a script
failure. The device also sleeps and drops WiFi periodically, so the whole
run retries a few times before treating it as actually unreachable.

Usage:
  python3 scripts/fetch_device_logs.py                      # crosspoint.local, ./device_logs/
  python3 scripts/fetch_device_logs.py --host 192.168.0.143
  python3 scripts/fetch_device_logs.py --out /tmp/logs --generations 5
"""
import argparse
import json
import time
import urllib.error
import urllib.request
from pathlib import Path

DEFAULT_HOST = "crosspoint.local"
DEFAULT_OUT = Path("device_logs")
# Must match DiskLogger::MAX_LOG_GENERATIONS (src/DiskLogger.h): debug.log (active)
# plus this many rotated backups (debug.log.1 .. debug.log.<N-1>).
DEFAULT_GENERATIONS = 5
CRASH_REPORT_REMOTE = "/crash_report.txt"


def fetch_one(host: str, remote_path: str, timeout: float = 10.0, retries: int = 2, delay: float = 2.0):
    """Returns (bytes, None) on success, (None, "missing") on 404, (None, reason) on
    unreachable after retries. A 404 is not retried -- it's a real answer, not a
    transient failure."""
    url = f"http://{host}{remote_path}"
    last_reason = "unknown error"
    for attempt in range(1, retries + 1):
        try:
            with urllib.request.urlopen(url, timeout=timeout) as resp:
                return resp.read(), None
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None, "missing"
            last_reason = f"HTTP {e.code}"
        except (urllib.error.URLError, OSError, TimeoutError) as e:
            last_reason = str(e)
        if attempt < retries:
            time.sleep(delay)
    return None, last_reason


def check_device(host: str, retries: int = 3, delay: float = 3.0) -> dict | None:
    for attempt in range(1, retries + 1):
        try:
            with urllib.request.urlopen(f"http://{host}/api/status", timeout=8) as resp:
                return json.loads(resp.read().decode())
        except (urllib.error.URLError, OSError, TimeoutError) as e:
            print(f"  [{attempt}/{retries}] {host} not responding yet ({e})")
            if attempt < retries:
                time.sleep(delay)
    return None


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--host", default=DEFAULT_HOST, help=f"Device host/IP (default: {DEFAULT_HOST})")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT, help=f"Output directory (default: {DEFAULT_OUT})")
    parser.add_argument(
        "--generations",
        type=int,
        default=DEFAULT_GENERATIONS,
        help=f"debug.log + this many rotated generations to try (default: {DEFAULT_GENERATIONS})",
    )
    parser.add_argument("--retries", type=int, default=3, help="Connectivity retries before giving up (default: 3)")
    args = parser.parse_args()

    print(f"Checking device at {args.host}...")
    status = check_device(args.host, retries=args.retries)
    if not status:
        print(f"\n{args.host} did not respond after {args.retries} attempts.")
        print("The device sleeps and drops WiFi -- wake it (press a button) and try again,")
        print("or pass --host <device-ip> if crosspoint.local isn't resolving.")
        raise SystemExit(1)
    print(f"  reachable: version={status.get('version')} rssi={status.get('rssi')} uptime={status.get('uptime')}")

    targets = [(CRASH_REPORT_REMOTE, "crash_report.txt")]
    for gen in range(args.generations):
        remote = "/.crosspoint/debug.log" if gen == 0 else f"/.crosspoint/debug.log.{gen}"
        local = "debug.log" if gen == 0 else f"debug.log.{gen}"
        targets.append((remote, local))

    args.out.mkdir(parents=True, exist_ok=True)
    fetched, missing, failed = [], [], []
    for remote_path, local_name in targets:
        data, reason = fetch_one(args.host, remote_path, retries=args.retries)
        if data is not None:
            dest = args.out / local_name
            dest.write_bytes(data)
            print(f"  ok       {remote_path} -> {dest} ({len(data)} bytes)")
            fetched.append(local_name)
        elif reason == "missing":
            print(f"  skip     {remote_path} (not present on device)")
            missing.append(local_name)
        else:
            print(f"  FAILED   {remote_path} ({reason})")
            failed.append(local_name)

    print(f"\n{len(fetched)} fetched, {len(missing)} not present, {len(failed)} failed. Output: {args.out}/")
    if not fetched and failed:
        # Reachable for /api/status but every single log request failed -- worth a
        # non-zero exit even though individual missing files are fine on their own.
        raise SystemExit(1)


if __name__ == "__main__":
    main()
