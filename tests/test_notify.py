#!/usr/bin/env python3
"""Notifications end to end, against a stand-in notification server on a
private session bus (dbus-run-session), so nothing appears on the real
desktop. Checks that the text reaches the server over D-Bus with its markup
escaped, that actions come back, and that a stopped call closes its toast.

Run from the plugin folder:  python3 -m unittest discover -s tests"""
import json
import os
import pathlib
import shutil
import signal
import subprocess
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
BACKEND = [sys.executable, "-I", str(ROOT / "backend.py"), "request"]
CALL = {"cmd": "notify", "kind": "call", "path": "/org/bluez/hci0/call1",
        "name": "<b>Eve</b> & co", "number": "+15551234567"}


def inside():
    """Runs on the private bus: serve org.freedesktop.Notifications, drive the
    backend against it, and exit non-zero on the first wrong observation."""
    import gi
    gi.require_version("Gio", "2.0")
    from gi.repository import Gio, GLib

    node = Gio.DBusNodeInfo.new_for_xml("""<node><interface name="org.freedesktop.Notifications">
      <method name="GetCapabilities"><arg type="as" direction="out"/></method>
      <method name="Notify"><arg type="s" direction="in"/><arg type="u" direction="in"/>
        <arg type="s" direction="in"/><arg type="s" direction="in"/><arg type="s" direction="in"/>
        <arg type="as" direction="in"/><arg type="a{sv}" direction="in"/><arg type="i" direction="in"/>
        <arg type="u" direction="out"/></method>
      <method name="CloseNotification"><arg type="u" direction="in"/></method>
      <signal name="ActionInvoked"><arg type="u"/><arg type="s"/></signal>
      <signal name="NotificationClosed"><arg type="u"/><arg type="u"/></signal>
    </interface></node>""")
    seen = {"notify": [], "closed": []}
    loop = GLib.MainLoop()
    bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
    on_notify = []

    def method(conn, sender, path, iface, name, params, invocation):
        if name == "GetCapabilities":
            invocation.return_value(GLib.Variant("(as)", (["body", "body-markup", "actions"],)))
        elif name == "Notify":
            seen["notify"].append(params.unpack())
            invocation.return_value(GLib.Variant("(u)", (len(seen["notify"]),)))
            for hook in on_notify:
                hook(len(seen["notify"]))
        else:
            seen["closed"].append(params.unpack()[0])
            invocation.return_value(None)

    bus.register_object("/org/freedesktop/Notifications", node.interfaces[0], method, None, None)
    Gio.bus_own_name_on_connection(bus, "org.freedesktop.Notifications", Gio.BusNameOwnerFlags.NONE, None, None)

    env = dict(os.environ, XDG_RUNTIME_DIR=tempfile.mkdtemp())

    def run(request, hook=None):
        """Start the backend with `request` on stdin; return its output once
        it exits. `hook(id, process)` runs when its notification arrives."""
        del on_notify[:]
        p = subprocess.Popen(BACKEND, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, env=env)
        if hook:
            on_notify.append(lambda nid: hook(nid, p))
        p.stdin.write((json.dumps(request) + "\n").encode())
        p.stdin.close()

        def poll():
            if p.poll() is not None:
                loop.quit()
            return True

        sources = [GLib.timeout_add(50, poll), GLib.timeout_add_seconds(10, lambda: loop.quit() or True)]
        loop.run()
        for source in sources:
            GLib.source_remove(source)
        if p.poll() is None:
            p.kill()
            raise AssertionError("backend did not exit for %r" % request)
        return p.stdout.read().decode().splitlines(), p.stderr.read().decode()

    def check(condition, message):
        if not condition:
            print("FAIL:", message, seen, file=sys.stderr)
            sys.exit(1)

    # A call: text over the bus, markup escaped, the chosen action printed.
    def answer(nid, _p):
        GLib.timeout_add(100, lambda: bus.emit_signal(
            None, "/org/freedesktop/Notifications", "org.freedesktop.Notifications",
            "ActionInvoked", GLib.Variant("(us)", (nid, "answer"))) and False)

    out, err = run(CALL, answer)
    check(err == "", "backend wrote to stderr: %r" % err)
    check(out and out[0] == "answer", "action was not printed: %r" % out)
    app, _, icon, summary, body, actions, hints, _ = seen["notify"][0]
    check(summary == "Incoming call", "summary %r" % summary)
    check(body == "&lt;b&gt;Eve&lt;/b&gt; &amp; co · +15551234567", "body not escaped: %r" % body)
    check(actions == ["answer", "Answer", "hangup", "Decline"], "actions %r" % actions)
    check(hints.get("urgency") == 2, "urgency %r" % hints)

    # An action this widget never offered is not passed on.
    def forged(nid, _p):
        GLib.timeout_add(100, lambda: bus.emit_signal(
            None, "/org/freedesktop/Notifications", "org.freedesktop.Notifications",
            "ActionInvoked", GLib.Variant("(us)", (nid, "rm -rf"))) and False)

    out, _ = run(CALL, forged)
    check(not any("rm" in line for line in out[:-1]), "forged action printed: %r" % out)

    # The call ended: stopping the backend takes the toast down.
    out, _ = run(CALL, lambda nid, p: GLib.timeout_add(200, lambda: p.send_signal(signal.SIGTERM) and False))
    check(seen["closed"] == [3], "toast was not closed on SIGTERM: %r" % seen["closed"])

    # Pairing: shown once per window, however many copies ask.
    pair = {"cmd": "notify", "kind": "pairing", "fingerprint": "ab" * 32, "name": "Phone <i>x</i>"}
    first, _ = run(pair)
    second, _ = run(pair)
    check(json.loads(first[-1]) == {"ok": True, "shown": True}, "first pairing %r" % first)
    check(json.loads(second[-1]) == {"ok": True, "shown": False}, "second pairing %r" % second)
    check(len(seen["notify"]) == 4, "pairing shown %d times" % (len(seen["notify"]) - 3))
    check(seen["notify"][3][3] == "Pairing request", "pairing summary")
    check(seen["notify"][3][4].startswith('A device calling itself "Phone &lt;i&gt;x&lt;/i&gt;" asks to pair'), "pairing body")
    # Shown, but kept out of the notification history on disk.
    check(all(n[6].get("transient") is True for n in seen["notify"]), "a notification is not transient")
    shutil.rmtree(env["XDG_RUNTIME_DIR"], ignore_errors=True)
    sys.exit(0)


@unittest.skipUnless(shutil.which("dbus-run-session"), "needs dbus-run-session")
class Notifications(unittest.TestCase):
    def test_against_a_private_bus(self):
        try:
            import gi  # noqa: F401
        except ImportError:
            self.skipTest("needs python-gobject")
        r = subprocess.run(["dbus-run-session", "--", sys.executable, __file__, "--inside"],
                           capture_output=True, text=True, timeout=120)
        self.assertEqual(r.returncode, 0, r.stderr)


if __name__ == "__main__":
    inside() if sys.argv[1:] == ["--inside"] else unittest.main()
