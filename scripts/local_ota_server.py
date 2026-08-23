#!/usr/bin/env python3
"""
Serve the locally-built `default` env firmware.bin as a self-hosted OTA
manifest, so a device on personal-integration can pull it via
OtaUpdater's OTA_MANIFEST_URL override instead of GitHub Releases.

The device only compares the numeric major.minor.patch prefix of tag_name
against its compiled-in CROSSPOINT_VERSION (see OtaUpdater::isUpdateNewer in
src/network/OtaUpdater.cpp), and the `default` env's version is always
"<base>-<date>-<branch>-<sha>" with the base's patch taken verbatim from
platformio.ini's [crosspoint] version -- so a static tag_name would only
trigger an update once, the first time the base version happens to be
lower. To make every changed build look newer, this server hashes
firmware.bin on each manifest request and bumps a persisted patch counter
(stored in .pio/ota_server_state.json, itself already gitignored via .pio/)
only when the hash actually changes, starting one patch above the repo's
base version. Restarting the server does not reset or regress the counter.
The rest of the tag (date/branch/sha, same shape as scripts/git_branch.py's
CROSSPOINT_VERSION) is cosmetic -- it makes the served build identifiable on
the device's OTA screen, but only the bumped major.minor.patch drives the
update-available check.

Usage:
  python3 scripts/local_ota_server.py                  # serves .pio/build/default/firmware.bin on :8091
  python3 scripts/local_ota_server.py --port 9000
  python3 scripts/local_ota_server.py --build-dir .pio/build/gh_release

Then point the device's build at it via the gitignored platformio.local.ini,
adding to [env:default] build_flags (see OtaUpdater.cpp's own comment):
  -DOTA_MANIFEST_URL=\\"http://<this-machine-ip>:8091/latest.json\\"
"""
import argparse
import datetime
import hashlib
import http.server
import json
import re
import socket
import subprocess
import sys
import threading
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

DEFAULT_PORT = 8091
DEFAULT_BUILD_DIR = REPO_ROOT / ".pio" / "build" / "default"
DEFAULT_STATE_FILE = REPO_ROOT / ".pio" / "ota_server_state.json"
MANIFEST_PATH = "/latest.json"
FIRMWARE_PATH = "/firmware.bin"

# Guards concurrent GETs against the state file (ThreadingHTTPServer runs one
# thread per request).
state_lock = threading.Lock()


def run_git_value(args: list[str], fallback: str = "unknown") -> str:
    try:
        value = subprocess.check_output(
            ["git", *args], text=True, stderr=subprocess.DEVNULL, cwd=REPO_ROOT
        ).strip()
        # Strip characters that would break a C string literal / JSON string.
        return "".join(c for c in value if c not in '"\\')
    except (OSError, subprocess.CalledProcessError):
        return fallback


def get_git_branch() -> str:
    branch = run_git_value(["rev-parse", "--abbrev-ref", "HEAD"])
    return "detached" if branch == "HEAD" else branch


def get_git_short_sha() -> str:
    return run_git_value(["rev-parse", "--short", "HEAD"])


def get_base_version() -> str:
    ini_text = (REPO_ROOT / "platformio.ini").read_text()
    match = re.search(r"^\[crosspoint\]\s*\nversion\s*=\s*(\S+)", ini_text, re.MULTILINE)
    if not match:
        sys.exit("Could not find [crosspoint] version in platformio.ini")
    return match.group(1)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_state(state_file: Path):
    if not state_file.exists():
        return None
    try:
        return json.loads(state_file.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def save_state(state_file: Path, state: dict) -> None:
    state_file.parent.mkdir(parents=True, exist_ok=True)
    state_file.write_text(json.dumps(state))


_NUMERIC_PREFIX_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)")


def resolve_version(base_version: str, state: dict | None, firmware_hash: str) -> tuple[str, dict]:
    """Return (tag_name, new_state), bumping the patch only when the hash changed.

    The emitted tag embeds the build date, git branch, and short SHA (same
    shape as scripts/git_branch.py's CROSSPOINT_VERSION) so the OTA screen
    shows which build is on offer. isUpdateNewer only compares the numeric
    major.minor.patch prefix, so the patch still has to be bumped on every
    hash change -- otherwise two same-day builds on the same branch would
    look identical to that comparison.
    """
    base_major, base_minor, base_patch = (int(part) for part in base_version.split(".")[:3])

    if state and state.get("sha256") == firmware_hash:
        return state["version"], state

    prev_version = state.get("version") if state else None
    prev_match = _NUMERIC_PREFIX_RE.match(prev_version) if prev_version else None
    if prev_match:
        prev_major, prev_minor, prev_patch = (int(g) for g in prev_match.groups())
    else:
        prev_major = prev_minor = prev_patch = None

    if prev_major == base_major and prev_minor == base_minor:
        new_patch = prev_patch + 1
    else:
        # First run, or platformio.ini's base version moved -- reset just
        # above the current base so it's still guaranteed newer than a
        # freshly-flashed device.
        new_patch = base_patch + 1

    date_stamp = datetime.date.today().strftime("%Y%m%d")
    branch = get_git_branch()
    short_sha = get_git_short_sha()
    new_version = f"{base_major}.{base_minor}.{new_patch}-{date_stamp}-{branch}-{short_sha}"
    return new_version, {"sha256": firmware_hash, "version": new_version}


def local_ip() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def make_handler(firmware_path: Path, state_file: Path, base_version: str):
    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            sys.stderr.write("[local_ota_server] " + (fmt % args) + "\n")

        def _manifest_body(self) -> bytes | None:
            if not firmware_path.exists():
                return None
            with state_lock:
                firmware_hash = sha256_file(firmware_path)
                state = load_state(state_file)
                version, new_state = resolve_version(base_version, state, firmware_hash)
                if new_state != state:
                    save_state(state_file, new_state)
            manifest = {
                "tag_name": version,
                "assets": [
                    {
                        "name": "firmware.bin",
                        "browser_download_url": f"http://{self.headers.get('Host', 'localhost')}{FIRMWARE_PATH}",
                        "size": firmware_path.stat().st_size,
                        "sha256": firmware_hash,
                    }
                ],
            }
            return json.dumps(manifest).encode()

        def do_GET(self):
            if self.path == MANIFEST_PATH:
                body = self._manifest_body()
                if body is None:
                    self.send_error(404, f"{firmware_path} not built yet -- run: pio run -e default")
                    return
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif self.path == FIRMWARE_PATH:
                if not firmware_path.exists():
                    self.send_error(404, f"{firmware_path} not built yet -- run: pio run -e default")
                    return
                data = firmware_path.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            else:
                self.send_error(404, "not found")

    return Handler


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--build-dir", default=str(DEFAULT_BUILD_DIR), help="dir containing firmware.bin (default: .pio/build/default)")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--host", default="0.0.0.0", help="bind address (default: 0.0.0.0)")
    parser.add_argument("--state-file", default=str(DEFAULT_STATE_FILE), help="version-bump state (default: .pio/ota_server_state.json)")
    args = parser.parse_args()

    firmware_path = Path(args.build_dir) / "firmware.bin"
    state_file = Path(args.state_file)
    base_version = get_base_version()

    handler = make_handler(firmware_path, state_file, base_version)
    server = http.server.ThreadingHTTPServer((args.host, args.port), handler)

    ip = local_ip()
    print(f"[local_ota_server] serving {firmware_path}")
    print(f"[local_ota_server] base version (platformio.ini): {base_version}")
    print(f"[local_ota_server] build tag: {get_git_branch()}/{get_git_short_sha()} (date-stamped per manifest request)")
    print(f"[local_ota_server] listening on {args.host}:{args.port}")
    print()
    print("Add to [env:default] build_flags in the gitignored platformio.local.ini:")
    print(f'  -DOTA_MANIFEST_URL=\\"http://{ip}:{args.port}{MANIFEST_PATH}\\"')
    print()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
