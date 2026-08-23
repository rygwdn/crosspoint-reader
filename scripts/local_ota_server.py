#!/usr/bin/env python3
"""
Serve the locally-built `default` env firmware.bin as a self-hosted OTA
manifest, so a device on personal-integration can pull it via
OtaUpdater's OTA_MANIFEST_URL override instead of GitHub Releases.

The device only compares the numeric major.minor.patch prefix of tag_name
against its compiled-in CROSSPOINT_VERSION (see OtaUpdater::isUpdateNewer in
src/network/OtaUpdater.cpp). The full CROSSPOINT_VERSION -- e.g.
"1.5.0-20260823-personal-integration-b5de1586" -- is baked into firmware.bin
itself: HttpDownloader.cpp concatenates "CrossPoint-ESP32-" CROSSPOINT_VERSION
into a single string literal for its HTTP User-Agent, which the compiler
always links in as one fixed, NUL-terminated anchor in .rodata regardless of
build env. This server reads that string back out of the built binary on
each manifest request instead of re-deriving it from the source tree's git
state, so the served tag always matches exactly what this firmware.bin will
report as its own version once flashed -- including when the repo has since
moved on to a different commit/branch than what was actually built.

Because the embedded major.minor.patch only changes when someone bumps
platformio.ini's [crosspoint] version, a static tag_name would only trigger
an update once, the first time that number happens to be lower than what's
on the device. To make every changed build look newer, this server hashes
firmware.bin on each manifest request and bumps a persisted patch counter
(stored in .pio/ota_server_state.json, itself already gitignored via .pio/)
only when the hash actually changes, starting one patch above the embedded
base version. Restarting the server does not reset or regress the counter.
The rest of the tag (date/branch/sha suffix) is taken verbatim from the
embedded string and is purely cosmetic -- only the bumped major.minor.patch
drives the update-available check.

Usage:
  python3 scripts/local_ota_server.py                  # serves .pio/build/default/firmware.bin on :8091
  python3 scripts/local_ota_server.py --port 9000
  python3 scripts/local_ota_server.py --build-dir .pio/build/gh_release

Then point the device's build at it via the gitignored platformio.local.ini,
adding to [env:default] build_flags (see OtaUpdater.cpp's own comment):
  -DOTA_MANIFEST_URL=\\"http://<this-machine-ip>:8091/latest.json\\"
"""
import argparse
import hashlib
import http.server
import json
import re
import socket
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


_EMBEDDED_VERSION_RE = re.compile(rb"CrossPoint-ESP32-([\x20-\x7e]+?)\x00")


class MissingEmbeddedVersion(RuntimeError):
    """firmware.bin has no CrossPoint-ESP32-<version> User-Agent literal."""


def extract_embedded_version(firmware_bytes: bytes) -> str:
    match = _EMBEDDED_VERSION_RE.search(firmware_bytes)
    if not match:
        raise MissingEmbeddedVersion(
            "no \"CrossPoint-ESP32-<version>\" literal found in firmware.bin -- "
            "is this a HttpDownloader-less build, or a stale/corrupt binary?"
        )
    return match.group(1).decode("ascii")


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


def resolve_version(embedded_version: str, state: dict | None, firmware_hash: str) -> tuple[str, dict]:
    """Return (tag_name, new_state), bumping the patch only when the hash changed.

    `embedded_version` is CROSSPOINT_VERSION as extracted from this exact
    firmware.bin (see extract_embedded_version) -- e.g.
    "1.5.0-20260823-personal-integration-b5de1586", or plain "1.5.0" for a
    gh_release-style build with no date/branch/sha suffix. isUpdateNewer only
    compares the numeric major.minor.patch prefix, so the patch still has to
    be bumped on every hash change -- otherwise two same-day builds on the
    same branch would look identical to that comparison. The suffix (if any)
    is carried through verbatim onto the bumped tag.
    """
    prefix_match = _NUMERIC_PREFIX_RE.match(embedded_version)
    if not prefix_match:
        raise MissingEmbeddedVersion(f"embedded version {embedded_version!r} has no major.minor.patch prefix")
    embedded_major, embedded_minor, embedded_patch = (int(g) for g in prefix_match.groups())
    suffix = embedded_version[prefix_match.end():]

    if state and state.get("sha256") == firmware_hash:
        return state["version"], state

    prev_version = state.get("version") if state else None
    prev_match = _NUMERIC_PREFIX_RE.match(prev_version) if prev_version else None
    if prev_match:
        prev_major, prev_minor, prev_patch = (int(g) for g in prev_match.groups())
    else:
        prev_major = prev_minor = prev_patch = None

    if prev_major == embedded_major and prev_minor == embedded_minor:
        new_patch = prev_patch + 1
    else:
        # First run, or the embedded base version moved -- reset just above
        # the current base so it's still guaranteed newer than a
        # freshly-flashed device.
        new_patch = embedded_patch + 1

    new_version = f"{embedded_major}.{embedded_minor}.{new_patch}{suffix}"
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


def make_handler(firmware_path: Path, state_file: Path):
    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            sys.stderr.write("[local_ota_server] " + (fmt % args) + "\n")

        def _manifest_body(self) -> bytes | None:
            if not firmware_path.exists():
                return None
            data = firmware_path.read_bytes()
            firmware_hash = hashlib.sha256(data).hexdigest()
            embedded_version = extract_embedded_version(data)
            with state_lock:
                state = load_state(state_file)
                version, new_state = resolve_version(embedded_version, state, firmware_hash)
                if new_state != state:
                    save_state(state_file, new_state)
            manifest = {
                "tag_name": version,
                "assets": [
                    {
                        "name": "firmware.bin",
                        "browser_download_url": f"http://{self.headers.get('Host', 'localhost')}{FIRMWARE_PATH}",
                        "size": len(data),
                        "sha256": firmware_hash,
                    }
                ],
            }
            return json.dumps(manifest).encode()

        def do_GET(self):
            if self.path == MANIFEST_PATH:
                try:
                    body = self._manifest_body()
                except MissingEmbeddedVersion as exc:
                    self.send_error(500, str(exc))
                    return
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

    handler = make_handler(firmware_path, state_file)
    server = http.server.ThreadingHTTPServer((args.host, args.port), handler)

    ip = local_ip()
    print(f"[local_ota_server] serving {firmware_path}")
    if firmware_path.exists():
        try:
            print(f"[local_ota_server] embedded version: {extract_embedded_version(firmware_path.read_bytes())}")
        except MissingEmbeddedVersion as exc:
            print(f"[local_ota_server] warning: {exc}")
    else:
        print("[local_ota_server] firmware.bin not built yet -- run: pio run -e default")
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
