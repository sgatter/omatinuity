#!/usr/bin/env python3
# Talks to tetherd over its local Unix socket ($XDG_RUNTIME_DIR/tether/tetherd.sock)
# using the daemon's newline-delimited JSON protocol, and prints one JSON object
# for the widget. Never starts the daemon on its own: a status poll must not
# resurrect a tetherd the user just stopped.
#
# Two rules hold throughout:
#   - Nothing private is ever an argument. Message text, numbers, names and
#     paths arrive as one JSON request on stdin and leave over the daemon's
#     socket or the session bus, because /proc/<pid>/cmdline is readable by
#     other local accounts.
#   - Everything the phone sends is untrusted. It is type-checked, bounded and
#     stripped of control characters before the widget sees it.
import fcntl
import hashlib
import html
import json
import os
import re
import select
import signal
import socket
import stat
import struct
import subprocess
import sys
import time
import unicodedata
import urllib.parse

PLUGIN_ID = "io.github.sgatter.omatinuity"

MAX_REQUEST = 256 * 1024        # one request on stdin
MAX_BUFFER = 4 * 1024 * 1024    # unread daemon output held at once
MAX_NAME = 120
MAX_NUMBER = 64
MAX_KEY = 256                   # thread keys, message handles, addresses
MAX_BODY = 8000                 # a message as shown
MAX_REPLY = 5000                # a message as sent
MAX_PREVIEW = 200
MAX_NOTE = 300                  # reasons and results from the daemon
MAX_PATH = 4096
MAX_CLIPBOARD = 1024 * 1024     # bytes of clipboard read to send to the phone
MAX_TYPES = 64 * 1024           # bytes of the clipboard's list of formats
CLIPBOARD_WAIT = 2.0            # seconds a clipboard owner gets to answer
MAX_TIME = 2 ** 40
MAX_FILES = 64

# Overrides, embeddings and isolates can make text read as something it is
# not, and iOS wraps phone numbers in embedding marks.
BIDI_CONTROLS = frozenset(range(0x202A, 0x202F)) | frozenset(range(0x2066, 0x206A))
BIDI_OVERRIDES = frozenset((0x202D, 0x202E))
LINE_BREAKS = "\r\n\x0b\x0c\x85\u2028\u2029"
# Format characters draw nothing. The two joiners stay: emoji and several
# scripts are built with them.
KEPT_FORMAT = frozenset((0x200C, 0x200D))
# Letters that draw nothing either; a name made of them would look empty.
BLANK_LETTERS = frozenset((0x115F, 0x1160, 0x3164, 0xFFA0))

# What a person dials: digits, an optional +, the usual separators. No service
# codes (* and #) and no pauses that send tones once connected: most numbers
# dialed here were supplied by the phone, not typed.
DIALABLE = re.compile(r"^\+?[0-9(][0-9 ().\-]{1,38}$")
CALL_PATH = re.compile(r"^/[A-Za-z0-9_/]{1,254}$")
FINGERPRINT = re.compile(r"^[A-Za-z0-9:+/=_-]{8,128}$")
# Omarchy names its captures screenshot-2026-11-17_20-31-05.png.
SCREENSHOT_NAME = re.compile(r"^screenshot[-_ ].{1,200}\.(png|jpe?g|webp)$", re.IGNORECASE)
IMAGE_NAME = re.compile(r"^.{1,240}\.(png|jpe?g|webp)$", re.IGNORECASE)


def clean(value, limit, multiline=False):
    """Untrusted text made safe to show: a str of at most `limit` characters
    with no control characters. One line unless `multiline`, and then also
    without characters that draw nothing, so a name cannot look empty or like
    another one."""
    if not isinstance(value, str):
        if value is None or isinstance(value, (bool, dict, list)):
            return ""
        value = str(value)
    out = []
    for ch in value[:limit * 4]:
        code = ord(ch)
        category = unicodedata.category(ch)
        if ch in LINE_BREAKS:
            out.append("\n" if multiline else " ")
        elif ch == "\t":
            out.append(" ")
        elif category in ("Cc", "Cs"):
            continue
        elif code in (BIDI_OVERRIDES if multiline else BIDI_CONTROLS):
            continue
        elif not multiline and ((category == "Cf" and code not in KEPT_FORMAT) or code in BLANK_LETTERS):
            continue
        else:
            out.append(ch)
    text = "".join(out)
    if multiline:
        text = text.replace("\n\n\n", "\n\n")
    return text[:limit].strip() if not multiline else text[:limit]


def ident(value, limit=MAX_KEY):
    """An identifier handed back to the daemon unchanged (thread key, message
    handle, address), or '' when it is not one."""
    if not isinstance(value, str) or not 0 < len(value) <= limit:
        return ""
    if any(unicodedata.category(ch) in ("Cc", "Cs") for ch in value):
        return ""
    return value


def count(value, high=2 ** 53):
    """An untrusted number as an int in 0..high."""
    try:
        n = int(value)
    except (TypeError, ValueError, OverflowError):
        return 0
    return max(0, min(high, n))


def rows(value, limit):
    """Up to `limit` objects from an untrusted list."""
    if not isinstance(value, list):
        return []
    return [v for v in value[:limit] if isinstance(v, dict)]


def want_thread(value):
    thread = ident(value)
    if not thread:
        raise ValueError("Not a conversation")
    return thread


def want_number(value):
    number = clean(value, MAX_NUMBER) if isinstance(value, str) else ""
    if not DIALABLE.match(number):
        raise ValueError("Not a phone number")
    return number


def want_call_path(value, required=False):
    if value in (None, "") and not required:
        return ""
    if not isinstance(value, str) or not CALL_PATH.match(value):
        raise ValueError("Not a call")
    return value


def want_fingerprint(value):
    if not isinstance(value, str) or not FINGERPRINT.match(value):
        raise ValueError("Not a device")
    return value


def want_paths(value):
    if not isinstance(value, list) or not 0 < len(value) <= MAX_FILES:
        raise ValueError("Nothing to send")
    for p in value:
        if not isinstance(p, str) or not 0 < len(p) <= MAX_PATH or "\0" in p:
            raise ValueError("Not a file path")
        # A relative path would be read against wherever the shell was started.
        if not os.path.isabs(p):
            raise ValueError("Give the full path of the file")
    return value


def runtime_base():
    return os.environ.get("XDG_RUNTIME_DIR") or "/run/user/%d" % os.getuid()


SOCKET_PATH = os.path.join(runtime_base(), "tether", "tetherd.sock")


class Daemon:
    def __init__(self, timeout=3.0):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(timeout)
        self.sock.connect(SOCKET_PATH)
        self.buf = b""
        # Only ever this user's own daemon, whatever the path points at.
        creds = self.sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
        if struct.unpack("3i", creds)[1] != os.getuid():
            self.close()
            raise OSError("tetherd socket belongs to another user")

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass

    def send(self, payload):
        self.sock.sendall((json.dumps(payload) + "\n").encode())

    def fill(self):
        """Read more from the daemon; False at end of stream."""
        chunk = self.sock.recv(65536)
        if not chunk:
            return False
        self.buf += chunk
        if len(self.buf) > MAX_BUFFER:
            raise OSError("Reply from tetherd is too large")
        return True

    def request(self, payload, want=None, deadline=3.0):
        """Send one command and return the first JSON reply whose command is in
        `want` (any JSON object when `want` is None). Stray broadcasts and the
        daemon's plain `OK` acks are skipped."""
        self.send(payload)
        end = time.monotonic() + deadline
        while time.monotonic() < end:
            while b"\n" in self.buf:
                line, self.buf = self.buf.split(b"\n", 1)
                line = line.strip()
                if not line or line == b"OK":
                    if want is None and line == b"OK":
                        return {"ok": True}
                    continue
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(obj, dict):
                    continue
                command = obj.get("command")
                if want is None or (isinstance(command, str) and command in want):
                    return obj
            if not self.fill():
                break
        raise TimeoutError("No reply to %s" % payload.get("command"))

    def request_line(self, end):
        """Next JSON object off the socket before monotonic time `end`, or None."""
        while time.monotonic() < end:
            if b"\n" in self.buf:
                line, self.buf = self.buf.split(b"\n", 1)
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                return obj if isinstance(obj, dict) else None
            try:
                if not self.fill():
                    return None
            except socket.timeout:
                return None
        return None


def unit_state():
    try:
        out = subprocess.run(
            ["systemctl", "--user", "is-active", "tetherd.service"],
            capture_output=True, text=True, timeout=3,
        )
        return clean(out.stdout, 32) or "unknown"
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def bt_setup_needed():
    """True until the one-time system setup (setup-bluetooth.sh) is in place:
    bluetoothd running --experimental and the Hands-Free class unit enabled.
    Without the class, iOS never offers the Messages/Contacts toggles."""
    try:
        exec_start = subprocess.run(["systemctl", "show", "bluetooth", "-p", "ExecStart", "--value"],
                                    capture_output=True, text=True, timeout=2).stdout
        btclass = subprocess.run(["systemctl", "is-enabled", "tether-btclass@hci0"],
                                 capture_output=True, text=True, timeout=2).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return False
    return "--experimental" not in exec_start or btclass != "enabled"


def installed():
    for d in os.environ.get("PATH", "").split(os.pathsep):
        if os.access(os.path.join(d, "tetherd"), os.X_OK):
            return True
    return False


def number_of(address):
    """A dialable number from a thread or contact address, or ''."""
    address = str(address or "")
    return clean(address[4:], MAX_NUMBER) if address.startswith("tel:") else ""


def thread_view(t):
    thread = ident(t.get("thread"))
    return {
        "thread": thread,
        "name": clean(t.get("name"), MAX_NAME) or clean(t.get("address"), MAX_NAME) or "Unknown",
        "preview": " ".join(clean(t.get("preview"), MAX_PREVIEW * 2).split())[:MAX_PREVIEW],
        "timestamp": count(t.get("timestamp"), MAX_TIME),
        "unread": count(t.get("unread"), 99999),
        "group": bool(t.get("group")),
        "repliable": t.get("repliable") is not False,
        "reason": clean(t.get("reply_reason"), MAX_NOTE),
        "number": number_of(thread),
    }


def call_view(c):
    path = c.get("path")
    return {
        "path": path if isinstance(path, str) and CALL_PATH.match(path) else "",
        "number": clean(c.get("number"), MAX_NUMBER),
        "name": clean(c.get("name"), MAX_NAME),
        "state": clean(c.get("state"), 32),
        "ringing": bool(c.get("ringing")),
        "outgoing": bool(c.get("outgoing")),
        "connected": bool(c.get("connected")),
        "withheld": bool(c.get("withheld")),
    }


def addresses_of(contact):
    """A contact's address strings, bounded."""
    found = contact.get("addresses")
    if not isinstance(found, list):
        return []
    return [a for a in (ident(a) for a in found[:16]) if a]


def contact_name(d, number):
    """Name for a caller the phone sent without one. iOS reports calls in the
    national form (0763…) while contacts hold +41763…, and tetherd's search
    misses across that leading 0, so search on the digits without it and keep
    only contacts whose number ends in them."""
    digits = "".join(ch for ch in str(number or "") if ch in "0123456789").lstrip("0")
    if len(digits) < 6:
        return ""
    try:
        r = d.request({"command": "bt_list_contacts", "query": digits, "limit": 10},
                      want={"bt_contacts"}, deadline=1.5)
    except (OSError, TimeoutError):
        return ""
    for c in rows(r.get("contacts"), 10):
        for a in addresses_of(c):
            if a.startswith("tel:") and "".join(ch for ch in a if ch in "0123456789").endswith(digits):
                return clean(c.get("name"), MAX_NAME)
    return ""


def messages(thread, mark=False):
    r = simple({"command": "bt_list_messages", "thread": thread}, {"bt_messages"})
    msgs = sorted(rows(r.get("messages"), 2000), key=lambda m: count(m.get("timestamp"), MAX_TIME))[-40:]
    out = [{
        "handle": ident(m.get("handle")),
        "body": clean(m.get("body"), MAX_BODY, multiline=True),
        "timestamp": count(m.get("timestamp"), MAX_TIME),
        "outgoing": bool(m.get("outgoing")),
        "read": m.get("read") is not False,
    } for m in msgs]
    # Opening a conversation reads it, as it would on the phone, and so does
    # replying. Reloads in the background do not: a panel left open must not
    # mark what nobody saw. mark_read writes through to the iPhone
    # asynchronously; nothing to wait for.
    unread = [m["handle"] for m in out if not m["outgoing"] and not m["read"] and m["handle"]]
    if mark and unread:
        d = Daemon()
        try:
            for h in unread:
                d.send({"command": "bt_mark_read", "handle": h, "read": True})
        finally:
            d.close()
    return {"ok": True, "thread": thread, "messages": out}


def reply(thread, body):
    if not isinstance(body, str) or not body.strip():
        return {"ok": False, "message": "Message is empty"}
    if len(body) > MAX_REPLY or "\0" in body:
        return {"ok": False, "message": "Message is too long"}
    op = "omarchy-%d" % int(time.time() * 1000)
    d = Daemon(timeout=20.0)
    try:
        # The result is a broadcast, so listen as a subscriber before sending.
        d.send({"command": "subscribe"})
        d.send({"command": "bt_send_message", "thread": thread, "body": body, "operation_id": op})
        end = time.monotonic() + 20.0
        while time.monotonic() < end:
            r = d.request_line(end)
            if r and r.get("command") == "bt_send_result" and r.get("operation_id") in (op, None):
                if r.get("success"):
                    return {"ok": True, "message": "Sent"}
                return {"ok": False,
                        "message": clean(r.get("message"), MAX_NOTE) or "The iPhone did not take the message"}
    finally:
        d.close()
    return {"ok": False, "message": "No answer from the iPhone"}


def contacts(query):
    r = simple({"command": "bt_list_contacts", "query": clean(query, MAX_NUMBER), "limit": 12}, {"bt_contacts"})
    out = []
    for c in rows(r.get("contacts"), 50):
        for a in addresses_of(c):
            # `address` goes back to the daemon unchanged; `label` is what
            # is shown for it.
            label = clean(a.split(":", 1)[1] if a.startswith(("tel:", "email:")) else a, MAX_NAME)
            out.append({"name": clean(c.get("name"), MAX_NAME) or label or "Unknown",
                        "address": a, "label": label, "number": number_of(a)})
    return {"ok": True, "contacts": out[:12]}


def call_history():
    r = simple({"command": "bt_list_call_history"}, {"bt_call_history"})
    calls = sorted(rows(r.get("calls"), 500), key=lambda c: -count(c.get("timestamp"), MAX_TIME))[:10]
    return {"ok": True, "calls": [{
        "number": clean(c.get("number"), MAX_NUMBER),
        "name": clean(c.get("name"), MAX_NAME),
        "type": clean(c.get("type"), 32),
        "timestamp": count(c.get("timestamp"), MAX_TIME),
    } for c in calls]}


def call_result(payload):
    r = simple(payload, {"bt_call_result"})
    return {"ok": bool(r.get("success")), "message": clean(r.get("message"), MAX_NOTE)}


def received_files(snap):
    """The files the daemon lists as received, newest first, without repeats."""
    seen = set()
    out = []
    for f in reversed(rows(snap.get("recent_received_files"), 200)):
        path = f.get("path")
        if not isinstance(path, str) or not os.path.isabs(path) or len(path) > MAX_PATH:
            continue
        if path in seen or clean(path, MAX_PATH) != path:
            continue
        seen.add(path)
        out.append({
            "path": path,
            "name": clean(f.get("filename"), 255) or os.path.basename(path),
            "bytes": count(f.get("bytes_written")),
            "exists": os.path.exists(path),
        })
    return out


def status():
    result = {
        "installed": installed(),
        "unit": unit_state(),
        "running": False,
        "clipboard": False,
        "mdns": False,
        "firewall": False,
        "devices": [],
        "pending": [],
        "received": [],
        "bt": None,
        "threads": [],
        "unread": 0,
        "calls": [],
        "phone_name": "",
        "error": "",
    }
    if not os.path.exists(SOCKET_PATH):
        return result

    try:
        d = Daemon()
    except OSError as e:
        result["error"] = clean("Socket unreachable: %s" % e, MAX_NOTE)
        return result

    try:
        snap = d.request({"command": "state_snapshot"}, want={"state_snapshot"})
        result["running"] = True
        result["clipboard"] = bool(snap.get("clipboard_available"))
        result["mdns"] = bool(snap.get("mdns_available"))
        result["firewall"] = bool(snap.get("firewall_active"))

        live = {}
        for c in rows(snap.get("connected_clients"), 64):
            fp = c.get("fingerprint")
            if isinstance(fp, str) and FINGERPRINT.match(fp) and c.get("paired"):
                live[fp] = c

        for p in rows(snap.get("paired_devices"), 64):
            fp = p.get("fingerprint")
            if not isinstance(fp, str) or not FINGERPRINT.match(fp):
                continue
            c = live.get(fp)
            result["devices"].append({
                "fingerprint": fp,
                "name": clean(p.get("device_name"), MAX_NAME) or "Unknown Device",
                "connected": c is not None,
                "address": clean((c or {}).get("address"), 64),
            })
        result["devices"].sort(key=lambda x: (not x["connected"], x["name"].lower()))

        for p in rows(snap.get("pending_pairs"), 16):
            fp = p.get("fingerprint")
            if not isinstance(fp, str) or not FINGERPRINT.match(fp):
                continue
            result["pending"].append({
                "fingerprint": fp,
                "name": clean(p.get("device_name"), MAX_NAME) or "Unknown Device",
            })

        result["received"] = received_files(snap)[:8]

        # Bluetooth is optional (messages/notifications); an old daemon or no
        # adapter just leaves it null.
        try:
            bt = d.request({"command": "bt_connection"},
                           want={"bt_connection", "bt_connection_changed"}, deadline=1.5)
            calls = bt.get("calls") if isinstance(bt.get("calls"), dict) else None
            result["bt"] = {
                "present": bool(bt.get("device_present")),
                "paired": bool(bt.get("device_paired")),
                "messages": bool(bt.get("map_open")),
                "contacts": bool(bt.get("pbap_open")),
                "notifications": bool(bt.get("ancs_ready")),
                "reason": clean(bt.get("profile_reason"), MAX_NOTE) or clean(bt.get("link_reason"), MAX_NOTE),
                "battery": count(bt.get("battery"), 100),
                # `available` is the gateway's own readiness (HFP state "connected");
                # `reason` is set even then, as an informational note.
                "calls_ready": calls is not None and calls.get("available") is True,
                "signal": count((calls or {}).get("signal"), 100),
                "calls_reason": clean((calls or {}).get("reason"), MAX_NOTE),
                "operator": clean((calls or {}).get("operator"), 64),
            }
        except (OSError, TimeoutError):
            pass

        # Messages and calls ride the Bluetooth link; with no link these just
        # come back empty, which the widget shows as "not set up".
        if result["bt"] is not None:
            result["bt"]["setup_needed"] = bt_setup_needed()
            try:
                st = d.request({"command": "bt_status"}, want={"bt_status"}, deadline=1.5)
                result["bt"]["calls_enabled"] = bool(st.get("calls_enabled"))
                # The Wi-Fi pairing only carries what the Tether app reports
                # ("iPhone"); Bluetooth knows the name set on the phone.
                address = st.get("device_address") or ""
                if address:
                    devs = d.request({"command": "bt_list_devices"}, want={"bt_devices"}, deadline=1.5)
                    for dev in rows(devs.get("devices"), 64):
                        if dev.get("address") == address and dev.get("name"):
                            result["phone_name"] = clean(dev.get("name"), MAX_NAME)
                            break
            except (OSError, TimeoutError):
                pass
            try:
                th = d.request({"command": "bt_list_threads"}, want={"bt_threads"}, deadline=2.0)
                threads = sorted(rows(th.get("threads"), 2000),
                                 key=lambda t: -count(t.get("timestamp"), MAX_TIME))
                result["unread"] = min(99999, sum(count(t.get("unread"), 99999) for t in threads))
                result["threads"] = [v for v in (thread_view(t) for t in threads[:8]) if v["thread"]]
            except (OSError, TimeoutError):
                pass
            try:
                calls = d.request({"command": "bt_list_calls"}, want={"bt_calls"}, deadline=1.5)
                result["calls"] = [c for c in (call_view(c) for c in rows(calls.get("calls"), 8)) if c["path"]]
                for c in result["calls"]:
                    if not c["name"] and c["number"] and not c["withheld"]:
                        c["name"] = contact_name(d, c["number"])
            except (OSError, TimeoutError):
                pass
    except (OSError, TimeoutError) as e:
        result["error"] = clean(str(e), MAX_NOTE)
    finally:
        d.close()

    return result


def simple(payload, want):
    d = Daemon()
    try:
        return d.request(payload, want=want, deadline=5.0)
    finally:
        d.close()


def bounded_output(argv, limit, wait):
    """What `argv` prints, read with a size limit and a time limit, so whoever
    feeds it (for wl-paste, the clipboard's owner) can neither fill memory nor
    keep this waiting. Returns (bytes, state): "eof" when it finished inside
    both limits, "limit" when it had more than `limit` bytes, "timeout" after
    `wait` seconds, "error" when it could not be started. Unless it finished,
    the process is killed."""
    try:
        p = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                             stderr=subprocess.DEVNULL)
    except OSError:
        return b"", "error"
    end = time.monotonic() + wait
    data, state = b"", "timeout"
    try:
        fd = p.stdout.fileno()
        while True:
            left = end - time.monotonic()
            if left <= 0:
                break
            ready, _, _ = select.select([fd], [], [], left)
            if not ready:
                break
            chunk = os.read(fd, min(65536, limit + 1 - len(data)))
            if not chunk:
                state = "eof"
                break
            data += chunk
            if len(data) > limit:
                state = "limit"
                break
    finally:
        if p.poll() is None:
            p.kill()
        p.stdout.close()
        try:
            p.wait(timeout=2)
        except subprocess.TimeoutExpired:
            pass
    return data[:limit], state


def clipboard_types():
    """The formats the clipboard offers."""
    data, state = bounded_output(["wl-paste", "--list-types"], MAX_TYPES, CLIPBOARD_WAIT)
    if state != "eof":
        return []
    return [clean(t, 255) for t in data.decode("utf-8", "replace").split()[:256]]


def clipboard_text(limit, primary=False):
    """(text, state) for the clipboard's text, or the middle-click selection's
    when `primary`: at most `limit` bytes of it, state as in bounded_output."""
    argv = ["wl-paste", "--no-newline", "--type", "text"]
    if primary:
        argv.append("--primary")
    data, state = bounded_output(argv, limit, CLIPBOARD_WAIT)
    # "ignore" also drops a character the limit cut in half.
    return data.decode("utf-8", "ignore"), state


def paste(primary=False):
    """Clipboard text for a text box in the panel. Qt is never asked for it:
    it would read on the shell's GUI thread with no limit of either kind."""
    text, state = clipboard_text(MAX_REPLY * 4, primary)
    if state == "timeout":
        return {"ok": False, "message": "The clipboard did not answer"}
    if state == "error":
        return {"ok": False, "message": "wl-paste is not available"}
    return {"ok": True, "text": clean(text, MAX_REPLY, multiline=True)}


def clipboard_files():
    """Paths of files copied in a file manager (text/uri-list on the clipboard)."""
    types = clipboard_types()
    mime = "text/uri-list" if "text/uri-list" in types else (
        "x-special/gnome-copied-files" if "x-special/gnome-copied-files" in types else None)
    if not mime:
        return []
    data, state = bounded_output(["wl-paste", "--no-newline", "--type", mime],
                                 MAX_CLIPBOARD, CLIPBOARD_WAIT)
    if state != "eof":
        return []
    paths = []
    for line in data.decode("utf-8", "replace").splitlines():
        line = line.strip()
        if line.startswith("file://"):
            p = urllib.parse.unquote(urllib.parse.urlparse(line).path)
            # Plain files only: a link could name one file and send another.
            if "\0" not in p and len(p) <= MAX_PATH and os.path.isfile(p) and not os.path.islink(p):
                paths.append(p)
        if len(paths) >= MAX_FILES:
            break
    return paths


def screenshot_folders():
    """(folder, dedicated) pairs to look for screenshots in. Omarchy saves them
    to OMASNAP_SCREENSHOT_DIR, else the older OMARCHY_SCREENSHOT_DIR, else
    Pictures/Screenshots. `dedicated` is False for Pictures itself, which
    older versions used and which also holds photos."""
    for name in ("OMASNAP_SCREENSHOT_DIR", "OMARCHY_SCREENSHOT_DIR"):
        folder = os.environ.get(name)
        if folder and os.path.isabs(folder):
            return [(folder, True)]
    data, state = bounded_output(["xdg-user-dir", "PICTURES"], MAX_PATH, 2.0)
    pictures = data.decode("utf-8", "replace").strip() if state == "eof" else ""
    if not os.path.isabs(pictures):
        pictures = os.path.expanduser("~/Pictures")
    return [(os.path.join(pictures, "Screenshots"), True), (pictures, False)]


def latest_screenshot(folders=None):
    """The newest screenshot, or None. In a folder that also holds other
    pictures only files named like a screenshot count, so a private photo is
    never picked; links are not followed, so a name cannot point elsewhere."""
    best, newest = None, -1.0
    for folder, dedicated in (screenshot_folders() if folders is None else folders):
        wanted = IMAGE_NAME if dedicated else SCREENSHOT_NAME
        try:
            with os.scandir(folder) as entries:
                for e in entries:
                    if not wanted.match(e.name) or not e.is_file(follow_symlinks=False):
                        continue
                    modified = e.stat(follow_symlinks=False).st_mtime
                    if modified > newest:
                        best, newest = e.path, modified
        except OSError:
            continue
    return best


def send(paths):
    paths = [os.path.abspath(p) for p in paths if p]
    paths = [p for p in paths if os.path.isfile(p)]
    if not paths:
        return {"ok": False, "message": "Nothing to send"}
    d = Daemon()
    try:
        for i, p in enumerate(paths):
            d.request({"command": "send_file", "path": p, "operation_id": "omarchy-%d" % i},
                      want=None, deadline=5.0)
    finally:
        d.close()
    names = ", ".join(clean(os.path.basename(p), 80) for p in paths[:3])
    more = " and %d more" % (len(paths) - 3) if len(paths) > 3 else ""
    return {"ok": True, "message": "Sending %s%s" % (names, more)}


def send_clipboard():
    # clipboard_send answers with tetherd's cached text, which stays empty until
    # the selection changes while the daemon runs (e.g. right after a restart).
    # When that happens, hand the daemon the live text with clipboard_set: it
    # becomes the selection again and tetherd pushes it to the phone.
    if any(t.startswith("image/") for t in clipboard_types()):
        simple({"command": "clipboard_send"}, {"clipboard_content"})
        return {"ok": True, "message": "Clipboard image sent to iPhone"}
    r = simple({"command": "clipboard_send"}, {"clipboard_content"})
    if r.get("content"):
        return {"ok": True, "message": "Clipboard sent to iPhone"}
    text, state = clipboard_text(MAX_CLIPBOARD)
    if state == "limit":
        return {"ok": False, "message": "Clipboard text is too large to send"}
    if state == "timeout":
        return {"ok": False, "message": "The clipboard did not answer"}
    if not text:
        return {"ok": False, "message": "Clipboard is empty"}
    d = Daemon()
    try:
        d.request({"command": "clipboard_set", "content": text}, want=None, deadline=3.0)
    finally:
        d.close()
    return {"ok": True, "message": "Clipboard sent to iPhone"}


def state_dir():
    """This plugin's private directory under the user's runtime directory, or
    None when there is no safe place for one. Never a shared temp directory."""
    path = os.path.join(runtime_base(), PLUGIN_ID)
    try:
        os.makedirs(path, mode=0o700, exist_ok=True)
        st = os.lstat(path)
    except OSError:
        return None
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid() or st.st_mode & 0o077:
        return None
    return path


def claim(key, window=0.0, directory=None):
    """Whether this copy of the widget shows the notification for `key`. The
    bar creates the widget once per monitor, so every copy sees the same event.
    With `window`, the first copy within that many seconds wins. Without, the
    first copy holds the lock until it exits (a call: for as long as it rings).
    Returns None when another copy has it, otherwise something to keep."""
    directory = directory or state_dir()
    if directory is None:
        return True
    name = hashlib.sha256(key.encode()).hexdigest()[:32] + ".lock"
    try:
        fd = os.open(os.path.join(directory, name),
                     os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    except OSError:
        return True
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        return None
    if not window:
        return fd
    try:
        last = float(os.read(fd, 32) or b"0")
    except ValueError:
        last = 0.0
    now = time.time()
    if 0 <= now - last < window:
        os.close(fd)
        return None
    os.lseek(fd, 0, os.SEEK_SET)
    os.ftruncate(fd, 0)
    os.write(fd, repr(now).encode())
    os.close(fd)
    return True


def session_bus():
    import gi
    gi.require_version("Gio", "2.0")
    from gi.repository import Gio, GLib
    return Gio, GLib, Gio.bus_get_sync(Gio.BusType.SESSION, None)


def on_terminate(GLib, handler):
    """Run `handler` from the main loop when this process is asked to stop."""
    try:
        import gi
        gi.require_version("GLibUnix", "2.0")
        from gi.repository import GLibUnix
        GLibUnix.signal_add(GLib.PRIORITY_DEFAULT, signal.SIGTERM, handler)
    except (ImportError, ValueError, AttributeError):
        GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGTERM, handler)


def notification_text(req):
    """(lock key, lock window, summary, body, icon, actions, urgency) for a
    notification request. The wording lives here so a request can only choose
    among the notifications this widget has."""
    kind = req.get("kind")
    if kind == "pairing":
        # Any device on the network can ask, under any name, as often as it
        # likes: the wording claims nothing about who it is, and one key for
        # all requests means one notification per half minute at most.
        want_fingerprint(req.get("fingerprint"))
        name = clean(req.get("name"), MAX_NAME)
        who = 'A device calling itself "%s"' % name if name else "A device"
        return ("pairing", 30.0, "Pairing request",
                "%s asks to pair with Tether. Open the phone panel to check it." % who,
                "/usr/share/icons/Adwaita/symbolic/devices/phone-symbolic.svg", [], None)
    if kind == "call":
        name = clean(req.get("name"), MAX_NAME)
        number = clean(req.get("number"), MAX_NUMBER)
        label = name or ("Unknown caller" if req.get("withheld") is True or not number else number)
        return ("call-" + want_call_path(req.get("path"), required=True), 0.0,
                "Incoming call", label + (" · " + number if name and number else ""),
                "/usr/share/icons/Adwaita/symbolic/actions/call-start-symbolic.svg",
                ["answer", "Answer", "hangup", "Decline"], 2)
    raise ValueError("Unknown notification")


def notify(req):
    """Show a notification over the session bus, so its text is never an
    argument. A call's notification stays until it is answered, declined,
    closed, or this process is stopped because the call ended; the chosen
    action is printed on its own line."""
    key, window, summary, body, icon, actions, urgency = notification_text(req)
    held = claim(key, window)
    if held is None:
        return {"ok": True, "shown": False}
    try:
        Gio, GLib, bus = session_bus()
    except (ImportError, ValueError):
        return {"ok": False, "message": "Notifications need python-gobject"}

    service = ("org.freedesktop.Notifications", "/org/freedesktop/Notifications",
               "org.freedesktop.Notifications")

    def call(method, args=None, reply=None):
        return bus.call_sync(*service, method, args, reply, Gio.DBusCallFlags.NONE, 3000, None)

    # The body is markup where the server says so; the summary never is.
    if "body-markup" in call("GetCapabilities", None, GLib.VariantType("(as)")).unpack()[0]:
        body = html.escape(body, quote=False)
    # Transient: a caller's name and number, or a name a stranger chose, are
    # shown but not written to the notification history on disk.
    hints = {"transient": GLib.Variant("b", True)}
    if urgency is not None:
        hints["urgency"] = GLib.Variant("y", urgency)
    shown = {"id": 0}
    loop = GLib.MainLoop()

    def on_signal(_bus, _sender, _path, _iface, name, params):
        values = params.unpack()
        if not shown["id"] or values[0] != shown["id"]:
            return
        if name == "ActionInvoked" and values[1] in ("answer", "hangup"):
            print(values[1], flush=True)
        loop.quit()

    def stop(*_):
        try:
            call("CloseNotification", GLib.Variant("(u)", (shown["id"],)))
        except GLib.Error:
            pass
        loop.quit()
        return False

    if actions:
        bus.signal_subscribe(service[0], service[2], None, service[1], None,
                             Gio.DBusSignalFlags.NONE, on_signal)
    shown["id"] = call("Notify", GLib.Variant("(susssasa{sv}i)", (
        "Tether", 0, icon, summary, body, actions, hints, -1)), GLib.VariantType("(u)")).unpack()[0]
    if actions:
        on_terminate(GLib, stop)
        GLib.timeout_add_seconds(180, stop)
        loop.run()
    return {"ok": True, "shown": True}


def show_file(path):
    """Reveal a received file in the file manager, over the session bus. Only
    a file the daemon itself lists as received, so this cannot be pointed at
    anything else."""
    d = Daemon()
    try:
        snap = d.request({"command": "state_snapshot"}, want={"state_snapshot"})
    finally:
        d.close()
    if not any(f["path"] == path and f["exists"] for f in received_files(snap)):
        return {"ok": False, "message": "That file is no longer there"}
    try:
        Gio, GLib, bus = session_bus()
        bus.call_sync("org.freedesktop.FileManager1", "/org/freedesktop/FileManager1",
                      "org.freedesktop.FileManager1", "ShowItems",
                      GLib.Variant("(ass)", (["file://" + urllib.parse.quote(path)], "")),
                      None, Gio.DBusCallFlags.NONE, 5000, None)
    except (ImportError, ValueError):
        return {"ok": False, "message": "Opening files needs python-gobject"}
    return {"ok": True, "message": "Shown in Files"}


WATCHED_EVENTS = {
    "bt_connection_changed", "bt_message", "bt_threads", "bt_call_result",
    "bt_send_result", "pair_request", "pair_accepted", "pair_rejected",
    "file_received", "file_end", "forget_device_result", "accept_device_result",
}


def watch():
    """Long-running: print one line per daemon event the widget cares about,
    so it can refresh at once instead of waiting for the next poll. Exits when
    the daemon goes away; the widget restarts it."""
    d = Daemon(timeout=None)
    d.send({"command": "subscribe"})
    print("ready", flush=True)
    try:
        while d.fill():
            while b"\n" in d.buf:
                line, d.buf = d.buf.split(b"\n", 1)
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                except RecursionError:
                    continue
                command = obj.get("command") if isinstance(obj, dict) else None
                if isinstance(command, str) and command in WATCHED_EVENTS:
                    print(command, flush=True)
    finally:
        d.close()


def read_request(stream):
    """The one JSON request on stdin: a bounded line holding an object."""
    line = stream.readline(MAX_REQUEST + 1)
    if len(line) > MAX_REQUEST:
        raise ValueError("Request is too large")
    try:
        req = json.loads(line.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise ValueError("Request is not valid JSON") from None
    if not isinstance(req, dict):
        raise ValueError("Request is not an object")
    return req


def handle(req):
    cmd = req.get("cmd")
    if cmd == "accept":
        r = simple({"command": "accept_device", "fingerprint": want_fingerprint(req.get("fingerprint"))},
                   {"accept_device_result"})
        return {"ok": bool(r.get("accepted")),
                "message": "Paired" if r.get("accepted") else "Pairing was not accepted"}
    if cmd == "forget":
        r = simple({"command": "forget_device", "fingerprint": want_fingerprint(req.get("fingerprint"))},
                   {"forget_device_result"})
        return {"ok": bool(r.get("forgotten")),
                "message": "Forgot device" if r.get("forgotten") else "Device was not paired"}
    if cmd == "clipboard":
        return send_clipboard()
    if cmd == "send":
        return send(want_paths(req.get("paths")))
    if cmd == "send-clipboard":
        files = clipboard_files()
        return send(files) if files else {"ok": False, "message": "No copied files on the clipboard"}
    if cmd == "send-screenshot":
        shot = latest_screenshot()
        return send([shot]) if shot else {"ok": False, "message": "No screenshot found"}
    if cmd == "messages":
        return messages(want_thread(req.get("thread")), req.get("mark") is True)
    if cmd == "reply":
        return reply(want_thread(req.get("thread")), req.get("body"))
    if cmd == "contacts":
        return contacts(req.get("query"))
    if cmd == "history":
        return call_history()
    if cmd == "dial":
        number = want_number(req.get("number"))
        out = call_result({"command": "bt_call_dial", "number": number})
        if out["ok"]:
            out["message"] = "Calling %s from the iPhone" % number
        return out
    if cmd in ("answer", "hangup"):
        out = call_result({"command": "bt_call_action", "action": cmd,
                           "path": want_call_path(req.get("path"))})
        if out["ok"]:
            out["message"] = "Answered — audio stays on the iPhone" if cmd == "answer" else "Call ended"
        return out
    if cmd == "paste":
        return paste(req.get("primary") is True)
    if cmd == "notify":
        return notify(req)
    if cmd == "show-file":
        path = req.get("path")
        if not isinstance(path, str):
            raise ValueError("Not a file path")
        return show_file(path)
    return {"ok": False, "message": "Unknown command"}


def main(argv):
    cmd = argv[1] if len(argv) > 1 else "status"
    if len(argv) > 2 or cmd not in ("status", "watch", "request"):
        # Refused rather than ignored, so data can never be passed this way.
        print(json.dumps({"ok": False, "message": "Requests are read from stdin, not arguments"}))
        return 2
    if cmd == "watch":
        try:
            watch()
        except Exception:
            # No traceback here either; the widget starts it again.
            pass
        return 0
    try:
        out = status() if cmd == "status" else handle(read_request(sys.stdin.buffer))
    except FileNotFoundError:
        out = {"ok": False, "message": "Tether daemon is not running"}
    except (OSError, TimeoutError, ValueError) as e:
        out = {"ok": False, "message": clean(str(e), MAX_NOTE) or e.__class__.__name__}
    except Exception:
        # No traceback: it could carry message text into the shell's log.
        out = {"ok": False, "message": "Tether action failed"}
    print(json.dumps(out))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
