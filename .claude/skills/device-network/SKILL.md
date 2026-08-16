---
name: device-network
description: Communicating with a physical CrossPoint Reader device over the local network -- browsing/deleting files, reading logs, and uploading a firmware build for the user to flash. Use whenever the user asks you to talk to their device, reach it via "WebDAV", pull debug logs off it, clear its cache, or push a firmware build to it.
---

# Talking to a CrossPoint Reader device over the network

The device exposes three distinct interfaces on the same host, all discovered
the hard way in a debugging session -- don't rediscover them:

1. An HTTP JSON/file-manager API on **port 80** (what the device's own web UI uses).
2. A real WebDAV server, also on **port 80** (PROPFIND/PUT/DELETE/MKCOL all work).
3. A custom binary WebSocket file-upload protocol on **port 81** (what the web
   UI's "Upload" button actually uses -- not WebDAV PUT).

## Finding the device

The user will usually give you an IP (e.g. `192.168.0.100`) or a `.local`
mDNS name. **Try the IP directly first.** mDNS (`*.local`) resolution
routinely fails in agent sandboxes even when the device is genuinely
reachable -- don't conclude "no network access" from an mDNS timeout alone.

The device sleeps to save power and periodically drops off WiFi; a failed
request doesn't mean it's gone. Retry a couple of times with a several-second
timeout before telling the user it's unreachable.

```bash
curl -s -m 10 "http://<ip>/api/status"
# {"version":"...","ip":"...","mode":"STA","rssi":-62,"freeHeap":...,"uptime":...,"device":"X3"}
```

Low RSSI (below about -85) predicts flaky large transfers (see Uploading, below).

## Browsing and editing files (plain curl, no library needed)

The JSON file API:

```bash
curl -s "http://<ip>/api/files?path=/"                    # list a directory
curl -s "http://<ip>/api/files?path=/.crosspoint"          # per-book caches live here
```

Delete one or more files (form-encoded JSON array of absolute paths):

```bash
curl -s -X POST "http://<ip>/delete" \
  -H "Content-Type: application/x-www-form-urlencoded" \
  --data-urlencode 'paths=["/some/file.bin","/other/file.bin"]'
```

Plain `curl -O "http://<ip>/path/to/file"` downloads any file directly.

WebDAV also works if you prefer standard verbs (`curl -T localfile
"http://<ip>/remote/path"` for PUT, `curl -X MKCOL ...` for directories) --
confirmed via `curl -X OPTIONS http://<ip>/` returning `DAV: 1`. It doesn't
save effort over the JSON API for small files; it matters mainly if you need
PROPFIND-style recursive listing.

## Book cache layout (for CSS/layout debugging)

`/.crosspoint/recent.json` maps each book's file path to its cache directory
(`epub_<hash>`) -- read this first rather than guessing the hash. Per book:

- `epub_<hash>/css_rules.cache` -- serialized parsed CSS rules.
- `epub_<hash>/sections/<spineIndex>.bin` -- cached page layout for one chapter.
- `epub_<hash>/progress.bin` -- reading position/bookmarks. **Don't delete this**
  when you just want to force a re-layout; delete `sections/*.bin` (and
  `css_rules.cache` if you suspect a CSS-parsing bug) instead.
- `epub_<hash>/book.bin` -- spine/TOC metadata cache.

Clearing `sections/<n>.bin` (or the whole `sections/` dir) forces that
chapter to be freshly parsed and laid out next time it's opened, without
touching the user's place in the book.

## Reading logs

The **live** debug log is root-level `/debug.log`, NOT `/.crosspoint/debug.log`
-- the latter path can exist as a stale leftover from an old build and will
quietly mislead you if you fetch it instead. Older sessions are kept as
`/debug.1.log` through `.4.log` (oldest last; see
`DiskLogger::getGenerationCount()` in `src/DiskLogger.h`), rotated one boot at
a time -- `.1.log` is the previous boot, not necessarily the previous crash.
Check line counts / timestamps across generations before trusting one.

Logging to this file is only flushed every ~16 log calls (see
`src/DiskLogger.h`), and only if `SETTINGS.diskLogsEnabled`. If you add
temporary `LOG_DBG` instrumentation to chase a bug, don't expect it to appear
until enough log lines have accumulated to trigger a flush -- pulling the log
immediately after a single action can show nothing new even though logging is
working.

```bash
curl -s -o debug.log "http://<ip>/debug.log"
grep "whatever you're chasing" debug.log
```

## Uploading a file (firmware builds, etc.)

Large uploads (firmware `.bin`, tens of KB+) need the WebSocket protocol on
port 81, not WebDAV PUT. Use the bundled script:

```bash
python3 .claude/skills/device-network/scripts/ws_upload.py <ip> <local_path> <remote_filename> [remote_dir]
```

This reimplements the exact protocol the device's own "Files" web UI uses:
WebSocket handshake -> text frame `START:<filename>:<size>:<dir>` -> wait for
`READY` -> binary frames of the file -> wait for `DONE`. The script has
built-in retries because **weak WiFi signal causes the connection to drop
mid-upload with a broken pipe** -- this is normal on this hardware, not a bug
in the script; just retry.

**Sandbox gotcha:** in some agent environments, Python's raw
`socket.connect()` fails with `OSError: [Errno 65] No route to host` for
non-standard ports (81) even though `curl`/`nc` reach the same host:port
instantly. If you ever need to hand-roll something like this again, tunnel
the raw TCP I/O through an `nc <host> <port>` subprocess's stdin/stdout
instead of using `socket` directly -- that's what `ws_upload.py` does, and
it's why it works when a naive socket-based client won't.

After uploading a firmware build, tell the user where it landed and let them
flash it themselves via **Settings -> Update Firmware from SD** on the
device. Don't overwrite the existing `update.bin` unless asked -- it may be
an intentionally-kept separate reference/stable build; upload as a clearly
named file (e.g. including a commit hash) instead.
