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

Requests go through `curl` rather than Python's urllib: in some sandboxed
environments, urllib's socket.getaddrinfo() fails to resolve .local mDNS
hostnames even though curl and the OS resolver succeed against the exact
same host -- same class of issue as the port-81 raw-socket problem documented
in ws_upload.py, just showing up for hostname resolution instead.

Usage:
  python3 scripts/fetch_device_logs.py                      # crosspoint.local, ./device_logs/
  python3 scripts/fetch_device_logs.py --host 192.168.0.143
  python3 scripts/fetch_device_logs.py --out /tmp/logs --generations 5
"""
import argparse
import json
import subprocess
import time
from pathlib import Path

DEFAULT_HOST = "crosspoint.local"
DEFAULT_OUT = Path("device_logs")
# Must match DiskLogger::MAX_LOG_GENERATIONS (src/DiskLogger.h): debug.log (active)
# plus this many rotated backups (debug.log.1 .. debug.log.<N-1>).
DEFAULT_GENERATIONS = 5
CRASH_REPORT_REMOTE = "/crash_report.txt"
STATUS_CHECK_TIMEOUT = 15.0
FETCH_TIMEOUT = 15.0


def curl_get(url: str, timeout: float, connect_timeout: float) -> tuple[bytes, int]:
    """Fetch url via curl. Returns (body, http_status); raises ConnectionError if curl
    itself couldn't complete the request at all (DNS failure, connection refused,
    timeout) -- a real HTTP error response (404, 500, ...) is not an exception here,
    it's a normal (body, code) return so callers can tell "device said no" apart from
    "couldn't even reach the device"."""
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


def fetch_one(host: str, remote_path: str, timeout: float = FETCH_TIMEOUT, retries: int = 2, delay: float = 2.0):
    """Returns (bytes, None) on success, (None, "missing") on 404, (None, reason) on
    unreachable after retries. A 404 is not retried -- it's a real answer, not a
    transient failure."""
    url = f"http://{host}{remote_path}"
    last_reason = "unknown error"
    for attempt in range(1, retries + 1):
        try:
            body, code = curl_get(url, timeout=timeout, connect_timeout=timeout)
            if code == 200:
                return body, None
            if code == 404:
                return None, "missing"
            last_reason = f"HTTP {code}"
        except ConnectionError as e:
            last_reason = str(e)
        if attempt < retries:
            time.sleep(delay)
    return None, last_reason


def check_device(host: str, retries: int = 3, delay: float = 3.0) -> dict | None:
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
