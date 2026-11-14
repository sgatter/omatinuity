#!/usr/bin/env python3
"""Regression tests for the two rules backend.py and its QML callers keep:
nothing private in process arguments, and nothing from the phone trusted.

Run from the plugin folder:  python3 -m unittest discover -s tests"""
import importlib.util
import io
import json
import os
import pathlib
import re
import socket
import subprocess
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.dont_write_bytecode = True
spec = importlib.util.spec_from_file_location("backend", ROOT / "backend.py")
backend = importlib.util.module_from_spec(spec)
spec.loader.exec_module(backend)

MARKUP = '<img src="https://example.invalid/tracker"><b>bold</b> &amp;'

# Every command the QML may start, exactly as written. None of them takes a
# value that comes from the phone, the clipboard or a text field.
ALLOWED_COMMANDS = {
    'backend.concat(["status"])',
    'backend.concat(["request"])',
    'root.backend.concat(["watch"])',
    '["wl-copy"]',
    '["systemctl", "--user", on ? "start" : "stop", "tetherd.service"]',
    '["sh", "-c", "uwsm-app -- nautilus \\"$(xdg-user-dir DOWNLOAD)\\""]',
    '["uwsm-app", "--", "tether-gtk"]',
    '["omarchy-launch-floating-terminal-with-presentation", '
    '"\'" + setupScript.replace(/\'/g, "\'\\\\\'\'") + "\'"]',
}


class NoPrivateArguments(unittest.TestCase):
    def test_qml_only_starts_allowlisted_commands(self):
        started = []
        for qml in sorted(ROOT.glob("*.qml")):
            for line in qml.read_text().splitlines():
                m = (re.search(r"\.command = (.+)$", line)
                     or re.match(r"\s*command: (.+)$", line)
                     or re.search(r"execDetached\((.+)\)$", line))
                if m:
                    started.append((qml.name, m.group(1).strip()))
        self.assertTrue(started)
        for name, command in started:
            self.assertIn(command, ALLOWED_COMMANDS, "%s starts an unlisted command" % name)

    def test_qml_names_no_argument_taking_notifier(self):
        for qml in ROOT.glob("*.qml"):
            self.assertNotIn("notify-send", qml.read_text(), qml.name)

    def run_backend(self, *args, stdin=b""):
        return subprocess.run([sys.executable, "-I", str(ROOT / "backend.py"), *args],
                              input=stdin, capture_output=True, timeout=10)

    def test_backend_refuses_data_as_arguments(self):
        for args in (("reply", "tel:+15551234567", "secret text"), ("dial", "+15551234567"),
                     ("messages", "tel:+15551234567"), ("request", "extra"), ("status", "x")):
            r = self.run_backend(*args)
            self.assertEqual(r.returncode, 2, args)
            self.assertFalse(json.loads(r.stdout)["ok"])
            self.assertNotIn(b"secret", r.stdout + r.stderr)

    def test_backend_reads_the_request_from_stdin(self):
        r = self.run_backend("request", stdin=b'{"cmd": "dial", "number": "not a number"}\n')
        self.assertEqual(json.loads(r.stdout), {"ok": False, "message": "Not a phone number"})

    def test_errors_carry_no_traceback(self):
        r = self.run_backend("request", stdin=b"\xff\xfe not json\n")
        self.assertEqual(r.stderr, b"")
        self.assertEqual(json.loads(r.stdout)["message"], "Request is not valid JSON")


class Requests(unittest.TestCase):
    def test_request_is_bounded(self):
        big = b'{"cmd": "reply", "body": "' + b"a" * backend.MAX_REQUEST + b'"}\n'
        with self.assertRaisesRegex(ValueError, "too large"):
            backend.read_request(io.BytesIO(big))

    def test_request_must_be_an_object(self):
        for raw in (b"[]\n", b'"x"\n', b"\n", b"{"):
            with self.assertRaises(ValueError):
                backend.read_request(io.BytesIO(raw))

    def test_unknown_command(self):
        self.assertFalse(backend.handle({"cmd": "rm -rf"})["ok"])

    def test_validators(self):
        self.assertEqual(backend.want_number("+41 76 123 45 67"), "+41 76 123 45 67")
        self.assertEqual(backend.want_number("‪+1 (555) 123-4567‬"), "+1 (555) 123-4567")
        self.assertEqual(backend.want_number("*100#"), "*100#")
        for bad in ("", "abc", "1; rm -rf ~", "$(id)", "1" * 41, None, 5, ["1"]):
            with self.assertRaises(ValueError, msg=repr(bad)):
                backend.want_number(bad)
        self.assertEqual(backend.want_thread("sender:sunrise"), "sender:sunrise")
        for bad in ("", "a" * 257, "tel:1\n2", "tel:\x00", None, {"x": 1}):
            with self.assertRaises(ValueError, msg=repr(bad)):
                backend.want_thread(bad)
        self.assertEqual(backend.want_call_path(""), "")
        self.assertEqual(backend.want_call_path("/org/bluez/hci0/dev_AA/call1"), "/org/bluez/hci0/dev_AA/call1")
        for bad in ("call1", "/a b", "/a;b", 3):
            with self.assertRaises(ValueError, msg=repr(bad)):
                backend.want_call_path(bad)
        with self.assertRaises(ValueError):
            backend.want_call_path("", required=True)
        for bad in ("", "short", "a b c d e f g h", "x" * 129, None):
            with self.assertRaises(ValueError, msg=repr(bad)):
                backend.want_fingerprint(bad)
        for bad in ([], "file", ["a\0b"], [5], ["x"] * (backend.MAX_FILES + 1)):
            with self.assertRaises(ValueError, msg=repr(bad)):
                backend.want_paths(bad)

    def test_reply_is_bounded_before_the_daemon_is_asked(self):
        self.assertEqual(backend.reply("tel:+1", "a" * (backend.MAX_REPLY + 1))["message"], "Message is too long")
        self.assertEqual(backend.reply("tel:+1", "  ")["message"], "Message is empty")
        self.assertEqual(backend.reply("tel:+1", None)["message"], "Message is empty")


class UntrustedText(unittest.TestCase):
    def test_markup_is_kept_as_text_never_interpreted(self):
        # The sinks are plain text (see PlainTextSinks); clean() must not turn
        # markup-shaped input into anything else on the way.
        self.assertEqual(backend.clean(MARKUP, 200), MARKUP)

    def test_control_characters_are_dropped(self):
        self.assertEqual(backend.clean("a\x00b\x1b[31mc\x7f\x9b", 50), "ab[31mc")
        self.assertEqual(backend.clean("one\r\ntwo\tthree four", 50), "one  two three four")
        self.assertEqual(backend.clean("one\ntwo", 50, multiline=True), "one\ntwo")
        self.assertEqual(backend.clean("\ud800x", 50), "x")

    def test_bidi_controls_cannot_reorder_names(self):
        self.assertEqual(backend.clean("evil‮gnp.exe", 50), "evilgnp.exe")
        self.assertEqual(backend.clean("⁦a⁩", 50), "a")
        self.assertEqual(backend.clean("a‮b⁧c", 50, multiline=True), "ab⁧c")

    def test_length_and_types(self):
        self.assertEqual(len(backend.clean("x" * 10000, 120)), 120)
        for value in (None, True, {"a": 1}, ["a"]):
            self.assertEqual(backend.clean(value, 50), "")
        self.assertEqual(backend.clean(42, 50), "42")

    def test_numbers(self):
        for value in ("x", None, float("nan"), float("inf"), [], -5):
            self.assertEqual(backend.count(value), 0)
        self.assertEqual(backend.count(10 ** 30, 100), 100)
        self.assertEqual(backend.count("7"), 7)

    def test_rows(self):
        self.assertEqual(backend.rows("nope", 5), [])
        self.assertEqual(backend.rows([{"a": 1}, "x", 3, {"b": 2}], 3), [{"a": 1}])
        self.assertEqual(len(backend.rows([{}] * 100, 8)), 8)

    def test_views_survive_hostile_shapes(self):
        hostile = {"thread": "tel:+1555", "name": ["x"], "preview": MARKUP + "\n" + "p" * 5000,
                   "timestamp": "soon", "unread": 10 ** 40, "reply_reason": {"a": 1}, "address": 7}
        view = backend.thread_view(hostile)
        self.assertEqual(view["name"], "7")
        self.assertEqual(view["timestamp"], 0)
        self.assertEqual(view["unread"], 99999)
        self.assertLessEqual(len(view["preview"]), backend.MAX_PREVIEW)
        self.assertNotIn("\n", view["preview"])
        self.assertEqual(backend.thread_view({"thread": "tel:1\x00"})["thread"], "")
        call = backend.call_view({"path": "/x; id", "number": "1" * 500, "name": "a\x1bb", "state": None})
        self.assertEqual((call["path"], len(call["number"]), call["name"]), ("", backend.MAX_NUMBER, "ab"))

    def test_received_files_must_be_plain_absolute_paths(self):
        snap = {"recent_received_files": [
            {"path": "relative.jpg"}, {"path": "/tmp/a\nb"}, {"path": 5}, "x",
            {"path": "/nonexistent/IMG_1.jpg", "filename": "IMG\x00_1.jpg", "bytes_written": "many"},
            {"path": "/nonexistent/IMG_1.jpg"},
        ]}
        self.assertEqual(backend.received_files(snap), [
            {"path": "/nonexistent/IMG_1.jpg", "name": "IMG_1.jpg", "bytes": 0, "exists": False}])


class PlainTextSinks(unittest.TestCase):
    """Markup-shaped input must never reach a rich-text sink: every text
    element the plugin owns declares a plain format."""

    def blocks(self, source):
        lines = source.splitlines()
        for i, line in enumerate(lines):
            if not re.match(r"\s*(Text|TextEdit) \{$", line):
                continue
            depth, block = 0, []
            for inner in lines[i:]:
                block.append(inner)
                depth += inner.count("{") - inner.count("}")
                if depth <= 0:
                    break
            yield i + 1, "\n".join(block)

    def test_every_text_element_is_plain(self):
        found = 0
        for qml in ROOT.glob("*.qml"):
            for line, block in self.blocks(qml.read_text()):
                found += 1
                self.assertRegex(block, r"textFormat: (Text|TextEdit)\.PlainText",
                                 "%s:%d has no plain textFormat" % (qml.name, line))
        self.assertGreater(found, 10)

    def test_no_rich_text_anywhere(self):
        for qml in ROOT.glob("*.qml"):
            for needle in ("RichText", "StyledText", "MarkdownText", "AutoText", "linkActivated",
                           "openUrlExternally"):
                self.assertNotIn(needle, qml.read_text(), qml.name)


class DaemonBounds(unittest.TestCase):
    def fake(self, payload):
        ours, theirs = socket.socketpair()
        d = backend.Daemon.__new__(backend.Daemon)
        d.sock, d.buf = ours, b""
        ours.settimeout(2)
        theirs.sendall(payload)
        theirs.close()
        self.addCleanup(d.close)
        return d

    def test_oversized_reply_is_refused(self):
        limit, backend.MAX_BUFFER = backend.MAX_BUFFER, 4096
        self.addCleanup(setattr, backend, "MAX_BUFFER", limit)
        d = self.fake(b"x" * 20000)
        with self.assertRaisesRegex(OSError, "too large"):
            while d.fill():
                pass

    def test_reply_lines_that_are_not_objects_are_skipped(self):
        d = self.fake(b'OK\n[1,2]\nnot json\n{"command": "other"}\n{"command": "wanted", "v": 1}\n')
        d.send = lambda payload: None
        self.assertEqual(d.request({"command": "x"}, want={"wanted"})["v"], 1)


class Claims(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(lambda: subprocess.run(["rm", "-rf", self.dir]))

    def test_window_lets_the_first_copy_through(self):
        self.assertIs(backend.claim("pair-x", 10.0, self.dir), True)
        self.assertIsNone(backend.claim("pair-x", 10.0, self.dir))
        self.assertIs(backend.claim("pair-y", 10.0, self.dir), True)

    def test_held_lock_lasts_until_released(self):
        fd = backend.claim("call-/a", 0.0, self.dir)
        self.assertIsInstance(fd, int)
        self.assertIsNone(backend.claim("call-/a", 0.0, self.dir))
        os.close(fd)
        again = backend.claim("call-/a", 0.0, self.dir)
        self.assertIsInstance(again, int)
        os.close(again)

    def test_lock_names_do_not_leak_the_key(self):
        backend.claim("call-/org/bluez/secret", 10.0, self.dir)
        self.assertFalse(any("secret" in name or "bluez" in name for name in os.listdir(self.dir)))

    def test_state_dir_must_be_private(self):
        base = os.path.join(self.dir, "run")
        os.makedirs(os.path.join(base, backend.PLUGIN_ID), mode=0o755)
        os.chmod(os.path.join(base, backend.PLUGIN_ID), 0o755)
        original = backend.runtime_base
        backend.runtime_base = lambda: base
        self.addCleanup(setattr, backend, "runtime_base", original)
        self.assertIsNone(backend.state_dir())
        os.chmod(os.path.join(base, backend.PLUGIN_ID), 0o700)
        self.assertEqual(backend.state_dir(), os.path.join(base, backend.PLUGIN_ID))


class NotificationText(unittest.TestCase):
    def test_a_request_cannot_choose_the_wording(self):
        key, window, summary, body, icon, actions, urgency = backend.notification_text(
            {"kind": "call", "path": "/org/bluez/call1", "name": MARKUP, "number": "+1555",
             "summary": "Your bank", "body": "Send money", "icon": "/etc/passwd"})
        self.assertEqual(summary, "Incoming call")
        self.assertEqual(body, MARKUP + " · +1555")
        self.assertTrue(icon.startswith("/usr/share/icons/"))
        self.assertEqual(actions, ["answer", "Answer", "hangup", "Decline"])

    def test_caller_labels(self):
        text = lambda **call: backend.notification_text(dict(kind="call", path="/c", **call))[3]
        self.assertEqual(text(number="+1555"), "+1555")
        self.assertEqual(text(number="+1555", withheld=True), "Unknown caller")
        self.assertEqual(text(), "Unknown caller")

    def test_invalid_requests(self):
        for req in ({"kind": "call", "path": "nope"}, {"kind": "pairing", "fingerprint": "x"}, {"kind": "other"}):
            with self.assertRaises(ValueError, msg=repr(req)):
                backend.notification_text(req)


if __name__ == "__main__":
    unittest.main()
