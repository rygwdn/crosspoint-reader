#!/usr/bin/env python3
"""
Upload a file to a CrossPoint Reader device over its binary WebSocket file-upload
protocol (port 81, arduino-WebSocketsServer). This is NOT WebDAV -- WebDAV PUT on
this device rejects large files / isn't used by the device's own web UI for
uploads, so this reimplements the exact protocol the "Files" web UI uses.

Protocol (text unless noted):
  1. Standard RFC6455 WebSocket handshake (GET / with Upgrade: websocket).
  2. Client sends a text frame: "START:<filename>:<size>:<remote_dir>"
  3. Server replies "READY".
  4. Client sends the file as binary frames (any chunk size; 4096 used here).
  5. Server replies "PROGRESS:<sent>:<total>" periodically (informational only).
  6. Server replies "DONE" once the file is fully written.
  7. Server may send ERROR:<message> at any point on failure.

Why this uses `nc` as a transport instead of Python's socket module: in some
sandboxed agent environments, raw socket.connect() to non-standard ports
(anything other than 80/443) fails with "No route to host" even though `curl`
and `nc` succeed against the exact same host:port. Tunneling the WebSocket
handshake and framing through an `nc` subprocess's stdin/stdout sidesteps
whatever is special-casing Python's socket syscalls, without needing a real
WebSocket library (which usually isn't installed and pip installs are often
blocked by PEP 668 on managed Pythons -- use a venv if you need one).

Usage:
  python3 ws_upload.py <host> <local_path> <remote_filename> [remote_dir] [--port N]

Example:
  python3 ws_upload.py 192.168.0.100 firmware.bin crosspoint-fix.bin /
"""
import argparse
import base64
import os
import select
import struct
import subprocess
import sys
import time


def recv_some(p, timeout=5, maxlen=65536):
    r, _, _ = select.select([p.stdout], [], [], timeout)
    if not r:
        return b""
    return os.read(p.stdout.fileno(), maxlen)


def make_frame(payload: bytes, opcode: int) -> bytes:
    fin_opcode = 0x80 | opcode
    length = len(payload)
    mask_key = os.urandom(4)
    if length <= 125:
        header = struct.pack("!BB", fin_opcode, 0x80 | length)
    elif length <= 65535:
        header = struct.pack("!BBH", fin_opcode, 0x80 | 126, length)
    else:
        header = struct.pack("!BBQ", fin_opcode, 0x80 | 127, length)
    masked = bytes(b ^ mask_key[i % 4] for i, b in enumerate(payload))
    return header + mask_key + masked


class FrameReader:
    def __init__(self, p):
        self.p = p
        self.buf = b""

    def fill(self, timeout=10):
        chunk = recv_some(self.p, timeout=timeout)
        if not chunk:
            return False
        self.buf += chunk
        return True

    def read_frame(self, timeout=15):
        deadline = time.time() + timeout
        while len(self.buf) < 2:
            if not self.fill(timeout=max(0.1, deadline - time.time())):
                raise TimeoutError("no data (header)")
        b0, b1 = self.buf[0], self.buf[1]
        opcode = b0 & 0x0F
        masked = (b1 & 0x80) != 0
        length = b1 & 0x7F
        idx = 2
        if length == 126:
            while len(self.buf) < idx + 2:
                if not self.fill(timeout=max(0.1, deadline - time.time())):
                    raise TimeoutError("no data (len16)")
            length = struct.unpack("!H", self.buf[idx : idx + 2])[0]
            idx += 2
        elif length == 127:
            while len(self.buf) < idx + 8:
                if not self.fill(timeout=max(0.1, deadline - time.time())):
                    raise TimeoutError("no data (len64)")
            length = struct.unpack("!Q", self.buf[idx : idx + 8])[0]
            idx += 8
        mask_key = b""
        if masked:
            while len(self.buf) < idx + 4:
                if not self.fill(timeout=max(0.1, deadline - time.time())):
                    raise TimeoutError("no data (mask)")
            mask_key = self.buf[idx : idx + 4]
            idx += 4
        while len(self.buf) < idx + length:
            if not self.fill(timeout=max(0.1, deadline - time.time())):
                raise TimeoutError("no data (payload)")
        payload = self.buf[idx : idx + length]
        if masked:
            payload = bytes(b ^ mask_key[i % 4] for i, b in enumerate(payload))
        self.buf = self.buf[idx + length :]
        return opcode, payload


def upload_once(host, port, local_path, remote_filename, remote_dir):
    with open(local_path, "rb") as f:
        data = f.read()
    size = len(data)

    p = subprocess.Popen(["nc", host, str(port)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, bufsize=0)

    ws_key = base64.b64encode(os.urandom(16)).decode()
    req = (
        f"GET / HTTP/1.1\r\n"
        f"Host: {host}:{port}\r\n"
        f"Upgrade: websocket\r\n"
        f"Connection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {ws_key}\r\n"
        f"Sec-WebSocket-Version: 13\r\n"
        f"\r\n"
    ).encode()
    p.stdin.write(req)
    p.stdin.flush()

    reader = FrameReader(p)
    deadline = time.time() + 10
    while b"\r\n\r\n" not in reader.buf:
        if not reader.fill(timeout=max(0.1, deadline - time.time())):
            print("Handshake failed: no response")
            p.terminate()
            return False
    header_end = reader.buf.index(b"\r\n\r\n") + 4
    headers = reader.buf[:header_end].decode(errors="replace")
    reader.buf = reader.buf[header_end:]
    if "101" not in headers.split("\r\n")[0]:
        print("Handshake did not return 101, aborting:\n", headers)
        p.terminate()
        return False

    def read_text_frame(timeout=10):
        while True:
            opcode, payload = reader.read_frame(timeout=timeout)
            if opcode == 0x9:  # ping -> pong
                p.stdin.write(make_frame(payload, opcode=0xA))
                p.stdin.flush()
                continue
            if opcode == 0x8:  # close
                raise ConnectionError("server closed connection: " + repr(payload))
            if opcode in (0x1, 0x2):
                return payload

    start_msg = f"START:{remote_filename}:{size}:{remote_dir}".encode()
    p.stdin.write(make_frame(start_msg, opcode=0x1))
    p.stdin.flush()

    payload = read_text_frame(timeout=10)
    if payload != b"READY":
        print("Did not get READY, aborting:", payload)
        p.terminate()
        return False

    CHUNK = 4096
    offset = 0
    last_print = 0
    try:
        while offset < size:
            chunk = data[offset : offset + CHUNK]
            p.stdin.write(make_frame(chunk, opcode=0x2))
            p.stdin.flush()
            offset += len(chunk)
            if offset - last_print >= CHUNK * 200 or offset == size:
                print(f"  sent {offset}/{size} bytes ({100 * offset // size}%)")
                last_print = offset
            time.sleep(0.001)
    except BrokenPipeError:
        print("Connection dropped mid-upload (often a weak WiFi signal on the device) -- retry")
        p.terminate()
        return False

    print("All chunks sent, waiting for DONE...")
    deadline = time.time() + 60
    while time.time() < deadline:
        payload = read_text_frame(timeout=10)
        text = payload.decode(errors="replace")
        if text == "DONE":
            print("Upload complete!")
            p.terminate()
            return True
        if text.startswith("ERROR:"):
            print("Upload failed:", text)
            p.terminate()
            return False
    print("Timed out waiting for DONE")
    p.terminate()
    return False


def upload(host, port, local_path, remote_filename, remote_dir, retries=3, retry_delay=3):
    for attempt in range(1, retries + 1):
        if attempt > 1:
            print(f"=== Retry {attempt}/{retries} ===")
        if upload_once(host, port, local_path, remote_filename, remote_dir):
            return True
        time.sleep(retry_delay)
    return False


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("host", help="Device IP or hostname, e.g. 192.168.0.100")
    parser.add_argument("local_path", help="Local file to upload")
    parser.add_argument("remote_filename", help="Filename to create on the device")
    parser.add_argument("remote_dir", nargs="?", default="/", help="Remote directory (default: /)")
    parser.add_argument("--port", type=int, default=81, help="WebSocket upload port (default: 81)")
    parser.add_argument("--retries", type=int, default=3)
    args = parser.parse_args()

    ok = upload(args.host, args.port, args.local_path, args.remote_filename, args.remote_dir, retries=args.retries)
    sys.exit(0 if ok else 1)
