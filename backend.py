#!/usr/bin/env python3
# Talks to tetherd over its local Unix socket ($XDG_RUNTIME_DIR/tether/tetherd.sock)
# using the daemon's newline-delimited JSON protocol, and prints one JSON object
# for the widget. Never starts the daemon on its own: a status poll must not
# resurrect a tetherd the user just stopped.
import json
import os
import socket
import subprocess
import sys
import time
import urllib.parse


def runtime_dir():
    base = os.environ.get("XDG_RUNTIME_DIR") or "/run/user/%d" % os.getuid()
    return os.path.join(base, "tether")


SOCKET_PATH = os.path.join(runtime_dir(), "tetherd.sock")


class Daemon:
    def __init__(self, timeout=3.0):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(timeout)
        self.sock.connect(SOCKET_PATH)
        self.buf = b""

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass

    def request(self, payload, want=None, deadline=3.0):
        """Send one command and return the first JSON reply whose command is in
        `want` (any JSON object when `want` is None). Stray broadcasts and the
        daemon's plain `OK` acks are skipped."""
        self.sock.sendall((json.dumps(payload) + "\n").encode())
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
                if want is None or obj.get("command") in want:
                    return obj
            chunk = self.sock.recv(65536)
            if not chunk:
                break
            self.buf += chunk
            if len(self.buf) > 4 * 1024 * 1024:
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
                chunk = self.sock.recv(65536)
            except socket.timeout:
                return None
            if not chunk:
                return None
            self.buf += chunk
        return None


def unit_state():
    try:
        out = subprocess.run(
            ["systemctl", "--user", "is-active", "tetherd.service"],
            capture_output=True, text=True, timeout=3,
        )
        return out.stdout.strip() or "unknown"
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
    return address[4:] if address.startswith("tel:") else ""


def thread_view(t):
    return {
        "thread": t.get("thread") or "",
        "name": t.get("name") or t.get("address") or "Unknown",
        "preview": " ".join(str(t.get("preview") or "").split()),
        "timestamp": int(t.get("timestamp") or 0),
        "unread": int(t.get("unread") or 0),
        "group": bool(t.get("group")),
        "repliable": t.get("repliable") is not False,
        "reason": t.get("reply_reason") or "",
        "number": number_of(t.get("thread")),
    }


def call_view(c):
    return {
        "path": c.get("path") or "",
        "number": c.get("number") or "",
        "name": c.get("name") or "",
        "state": c.get("state") or "",
        "ringing": bool(c.get("ringing")),
        "outgoing": bool(c.get("outgoing")),
        "connected": bool(c.get("connected")),
        "withheld": bool(c.get("withheld")),
    }


def contact_name(d, number):
    """Name for a caller the phone sent without one. iOS reports calls in the
    national form (0763…) while contacts hold +41763…, and tetherd's search
    misses across that leading 0, so search on the digits without it and keep
    only contacts whose number ends in them."""
    digits = "".join(ch for ch in str(number or "") if ch.isdigit()).lstrip("0")
    if len(digits) < 6:
        return ""
    try:
        r = d.request({"command": "bt_list_contacts", "query": digits, "limit": 10},
                      want={"bt_contacts"}, deadline=1.5)
    except (OSError, TimeoutError):
        return ""
    for c in r.get("contacts") or []:
        for a in c.get("addresses") or []:
            if a.startswith("tel:") and "".join(ch for ch in a if ch.isdigit()).endswith(digits):
                return c.get("name") or ""
    return ""


def messages(thread):
    r = simple({"command": "bt_list_messages", "thread": thread}, {"bt_messages"})
    msgs = sorted(r.get("messages") or [], key=lambda m: m.get("timestamp") or 0)[-40:]
    out = [{
        "handle": m.get("handle") or "",
        "body": m.get("body") or "",
        "timestamp": int(m.get("timestamp") or 0),
        "outgoing": bool(m.get("outgoing")),
        "read": m.get("read") is not False,
    } for m in msgs]
    # Opening a conversation reads it, as it would on the phone. mark_read
    # writes through to the iPhone asynchronously; nothing to wait for.
    unread = [m["handle"] for m in out if not m["outgoing"] and not m["read"] and m["handle"]]
    if unread:
        d = Daemon()
        try:
            for h in unread:
                d.sock.sendall((json.dumps({"command": "bt_mark_read", "handle": h, "read": True}) + "\n").encode())
        finally:
            d.close()
    return {"ok": True, "thread": thread, "messages": out}


def reply(thread, body):
    if not body.strip():
        return {"ok": False, "message": "Message is empty"}
    op = "omarchy-%d" % int(time.time() * 1000)
    d = Daemon(timeout=20.0)
    try:
        # The result is a broadcast, so listen as a subscriber before sending.
        d.sock.sendall(b'{"command":"subscribe"}\n')
        d.sock.sendall((json.dumps({"command": "bt_send_message", "thread": thread,
                                    "body": body, "operation_id": op}) + "\n").encode())
        end = time.monotonic() + 20.0
        while time.monotonic() < end:
            r = d.request_line(end)
            if r and r.get("command") == "bt_send_result" and r.get("operation_id") in (op, None):
                if r.get("success"):
                    return {"ok": True, "message": "Sent"}
                return {"ok": False, "message": r.get("message") or "The iPhone did not take the message"}
    finally:
        d.close()
    return {"ok": False, "message": "No answer from the iPhone"}


def contacts(query):
    r = simple({"command": "bt_list_contacts", "query": query, "limit": 12}, {"bt_contacts"})
    out = []
    for c in r.get("contacts") or []:
        for a in c.get("addresses") or []:
            out.append({"name": c.get("name") or a, "address": a, "number": number_of(a)})
    return {"ok": True, "contacts": out[:12]}


def call_history():
    r = simple({"command": "bt_list_call_history"}, {"bt_call_history"})
    calls = sorted(r.get("calls") or [], key=lambda c: -(c.get("timestamp") or 0))[:10]
    return {"ok": True, "calls": [{
        "number": c.get("number") or "",
        "name": c.get("name") or "",
        "type": c.get("type") or "",
        "timestamp": int(c.get("timestamp") or 0),
    } for c in calls]}


def call_result(payload):
    r = simple(payload, {"bt_call_result"})
    return {"ok": bool(r.get("success")), "message": r.get("message") or ""}


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
        result["error"] = "Socket unreachable: %s" % e
        return result

    try:
        snap = d.request({"command": "state_snapshot"}, want={"state_snapshot"})
        result["running"] = True
        result["clipboard"] = bool(snap.get("clipboard_available"))
        result["mdns"] = bool(snap.get("mdns_available"))
        result["firewall"] = bool(snap.get("firewall_active"))

        live = {}
        for c in snap.get("connected_clients") or []:
            fp = c.get("fingerprint") or ""
            if fp and c.get("paired"):
                live[fp] = c

        for p in snap.get("paired_devices") or []:
            fp = p.get("fingerprint") or ""
            c = live.get(fp)
            result["devices"].append({
                "fingerprint": fp,
                "name": p.get("device_name") or "Unknown Device",
                "connected": c is not None,
                "address": (c or {}).get("address") or "",
            })
        result["devices"].sort(key=lambda x: (not x["connected"], x["name"].lower()))

        for p in snap.get("pending_pairs") or []:
            result["pending"].append({
                "fingerprint": p.get("fingerprint") or "",
                "name": p.get("device_name") or "Unknown Device",
            })

        seen = set()
        for f in reversed(snap.get("recent_received_files") or []):
            path = f.get("path") or ""
            if not path or path in seen:
                continue
            seen.add(path)
            result["received"].append({
                "path": path,
                "name": f.get("filename") or os.path.basename(path),
                "bytes": int(f.get("bytes_written") or 0),
                "exists": os.path.exists(path),
            })
            if len(result["received"]) >= 8:
                break

        # Bluetooth is optional (messages/notifications); an old daemon or no
        # adapter just leaves it null.
        try:
            bt = d.request({"command": "bt_connection"},
                           want={"bt_connection", "bt_connection_changed"}, deadline=1.5)
            result["bt"] = {
                "present": bool(bt.get("device_present")),
                "paired": bool(bt.get("device_paired")),
                "messages": bool(bt.get("map_open")),
                "contacts": bool(bt.get("pbap_open")),
                "notifications": bool(bt.get("ancs_ready")),
                "reason": bt.get("profile_reason") or bt.get("link_reason") or "",
                "battery": int(bt.get("battery") or 0),
                # `available` is the gateway's own readiness (HFP state "connected");
                # `reason` is set even then, as an informational note.
                "calls_ready": isinstance(bt.get("calls"), dict) and bt["calls"].get("available") is True,
                "signal": int((bt.get("calls") or {}).get("signal") or 0) if isinstance(bt.get("calls"), dict) else 0,
                "calls_reason": (bt.get("calls") or {}).get("reason", "") if isinstance(bt.get("calls"), dict) else "",
                "operator": (bt.get("calls") or {}).get("operator", "") if isinstance(bt.get("calls"), dict) else "",
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
                    for dev in devs.get("devices") or []:
                        if dev.get("address") == address and dev.get("name"):
                            result["phone_name"] = dev["name"]
                            break
            except (OSError, TimeoutError):
                pass
            try:
                th = d.request({"command": "bt_list_threads"}, want={"bt_threads"}, deadline=2.0)
                threads = sorted(th.get("threads") or [], key=lambda t: -(t.get("timestamp") or 0))
                result["unread"] = sum(int(t.get("unread") or 0) for t in threads)
                result["threads"] = [thread_view(t) for t in threads[:8]]
            except (OSError, TimeoutError):
                pass
            try:
                calls = d.request({"command": "bt_list_calls"}, want={"bt_calls"}, deadline=1.5)
                result["calls"] = [call_view(c) for c in calls.get("calls") or []]
                for c in result["calls"]:
                    if not c["name"] and c["number"] and not c["withheld"]:
                        c["name"] = contact_name(d, c["number"])
            except (OSError, TimeoutError):
                pass
    except (OSError, TimeoutError) as e:
        result["error"] = str(e)
    finally:
        d.close()

    # With a single Wi-Fi pairing it is the same phone, so show its real name
    # there too; with several, only one of them can be the Bluetooth phone.
    if result["phone_name"] and len(result["devices"]) == 1:
        result["devices"][0]["name"] = result["phone_name"]
    return result


def simple(payload, want):
    d = Daemon()
    try:
        return d.request(payload, want=want, deadline=5.0)
    finally:
        d.close()


def clipboard_files():
    """Paths of files copied in a file manager (text/uri-list on the clipboard)."""
    try:
        types = subprocess.run(["wl-paste", "--list-types"], capture_output=True,
                               text=True, timeout=2).stdout.split()
        mime = "text/uri-list" if "text/uri-list" in types else (
            "x-special/gnome-copied-files" if "x-special/gnome-copied-files" in types else None)
        if not mime:
            return []
        raw = subprocess.run(["wl-paste", "--no-newline", "--type", mime],
                             capture_output=True, text=True, timeout=2).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    paths = []
    for line in raw.splitlines():
        line = line.strip()
        if line.startswith("file://"):
            p = urllib.parse.unquote(urllib.parse.urlparse(line).path)
            if os.path.isfile(p):
                paths.append(p)
    return paths


def latest_screenshot():
    d = os.environ.get("OMARCHY_SCREENSHOT_DIR")
    if not d:
        try:
            d = subprocess.run(["xdg-user-dir", "PICTURES"], capture_output=True,
                               text=True, timeout=2).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            d = ""
        d = d or os.path.expanduser("~/Pictures")
    best = None
    try:
        for e in os.scandir(d):
            if e.is_file() and e.name.lower().endswith((".png", ".jpg", ".jpeg", ".webp")):
                if best is None or e.stat().st_mtime > best.stat().st_mtime:
                    best = e
    except OSError:
        return None
    return best.path if best else None


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
    names = ", ".join(os.path.basename(p) for p in paths[:3])
    more = " and %d more" % (len(paths) - 3) if len(paths) > 3 else ""
    return {"ok": True, "message": "Sending %s%s" % (names, more)}


def clipboard_types():
    try:
        return subprocess.run(["wl-paste", "--list-types"], capture_output=True,
                              text=True, timeout=2).stdout.split()
    except (OSError, subprocess.SubprocessError):
        return []


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
    try:
        text = subprocess.run(["wl-paste", "--no-newline", "--type", "text"],
                              capture_output=True, text=True, timeout=2).stdout
    except (OSError, subprocess.SubprocessError):
        text = ""
    if not text:
        return {"ok": False, "message": "Clipboard is empty"}
    d = Daemon()
    try:
        d.request({"command": "clipboard_set", "content": text}, want=None, deadline=3.0)
    finally:
        d.close()
    return {"ok": True, "message": "Clipboard sent to iPhone"}


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
    d.sock.sendall(b'{"command":"subscribe"}\n')
    print("ready", flush=True)
    try:
        while True:
            chunk = d.sock.recv(65536)
            if not chunk:
                return
            d.buf += chunk
            while b"\n" in d.buf:
                line, d.buf = d.buf.split(b"\n", 1)
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                if isinstance(obj, dict) and obj.get("command") in WATCHED_EVENTS:
                    print(obj["command"], flush=True)
    finally:
        d.close()


def main(argv):
    if len(argv) > 1 and argv[1] == "watch":
        try:
            watch()
        except OSError:
            pass
        return 0
    cmd = argv[1] if len(argv) > 1 else "status"
    try:
        if cmd == "status":
            out = status()
        elif cmd == "accept":
            r = simple({"command": "accept_device", "fingerprint": argv[2]},
                       {"accept_device_result"})
            out = {"ok": bool(r.get("accepted")),
                   "message": "Paired" if r.get("accepted") else "Pairing was not accepted"}
        elif cmd == "forget":
            r = simple({"command": "forget_device", "fingerprint": argv[2]},
                       {"forget_device_result"})
            out = {"ok": bool(r.get("forgotten")),
                   "message": "Forgot device" if r.get("forgotten") else "Device was not paired"}
        elif cmd == "clipboard":
            out = send_clipboard()
        elif cmd == "send":
            out = send(argv[2:])
        elif cmd == "send-clipboard":
            files = clipboard_files()
            out = send(files) if files else {"ok": False, "message": "No copied files on the clipboard"}
        elif cmd == "send-screenshot":
            shot = latest_screenshot()
            out = send([shot]) if shot else {"ok": False, "message": "No screenshot found"}
        elif cmd == "messages":
            out = messages(argv[2])
        elif cmd == "reply":
            out = reply(argv[2], argv[3])
        elif cmd == "contacts":
            out = contacts(argv[2] if len(argv) > 2 else "")
        elif cmd == "history":
            out = call_history()
        elif cmd == "dial":
            out = call_result({"command": "bt_call_dial", "number": argv[2]})
            if out["ok"]:
                out["message"] = "Calling %s from the iPhone" % argv[2]
        elif cmd in ("answer", "hangup"):
            path = argv[2] if len(argv) > 2 else ""
            out = call_result({"command": "bt_call_action", "action": cmd, "path": path})
            if out["ok"]:
                out["message"] = "Answered — audio stays on the iPhone" if cmd == "answer" else "Call ended"
        else:
            out = {"ok": False, "message": "Unknown command: %s" % cmd}
    except FileNotFoundError:
        out = {"ok": False, "message": "Tether daemon is not running"}
    except (OSError, TimeoutError, IndexError) as e:
        out = {"ok": False, "message": str(e) or e.__class__.__name__}
    print(json.dumps(out))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
