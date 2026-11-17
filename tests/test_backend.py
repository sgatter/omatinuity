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
import time
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
    '["wl-copy", "--type", "text/plain"]',
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
        self.assertEqual(backend.want_number("(044) 123 45 67"), "(044) 123 45 67")
        self.assertEqual(backend.want_number("112"), "112")
        # Service codes and tones sent after connecting are not dialed: the
        # number usually comes from the phone, not from the keyboard.
        for bad in ("*100#", "*21*0900123456#", "#31#0441234567", "0441234567,,1234", "044p123", "044w1", "044;1",
                    "", "abc", "1; rm -rf ~", "$(id)", "1" * 41, "+", "5", None, 5, ["1"]):
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
        self.assertEqual(backend.want_paths(["/tmp/a b.txt"]), ["/tmp/a b.txt"])
        for bad in ([], "file", ["/a\0b"], [5], ["notes.txt"], ["./notes.txt"], ["~/notes.txt"],
                    ["/x"] * (backend.MAX_FILES + 1)):
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
        d = self.fake(b'OK\n[1,2]\nnot json\n{"command": "other"}\n{"command": []}\n{"command": {"a": 1}}\n'
                      b'{"command": "wanted", "v": 1}\n')
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


class BoundedReads(unittest.TestCase):
    """Whoever owns the clipboard feeds wl-paste; nothing read from it may be
    unbounded in size or in time."""

    def child(self, code):
        return [sys.executable, "-I", "-c", code]

    def test_finished_output_comes_back_whole(self):
        data, state = backend.bounded_output(self.child("print('hello', end='')"), 100, 5.0)
        self.assertEqual((data, state), (b"hello", "eof"))

    def test_endless_output_stops_at_the_limit(self):
        started = time.monotonic()
        data, state = backend.bounded_output(
            self.child("import sys\nwhile True: sys.stdout.buffer.write(b'x' * 65536)"), 200000, 30.0)
        self.assertEqual(state, "limit")
        self.assertEqual(len(data), 200000)
        self.assertLess(time.monotonic() - started, 10)

    def test_a_silent_owner_cannot_keep_it_waiting(self):
        started = time.monotonic()
        data, state = backend.bounded_output(self.child("import time\ntime.sleep(60)"), 100, 0.3)
        self.assertEqual((data, state), (b"", "timeout"))
        self.assertLess(time.monotonic() - started, 5)

    def test_a_trickle_cannot_renew_the_wait(self):
        # A byte every 50 ms would reset a per-read timeout forever.
        started = time.monotonic()
        data, state = backend.bounded_output(self.child(
            "import sys, time\nwhile True:\n    sys.stdout.buffer.write(b'x'); sys.stdout.buffer.flush(); time.sleep(0.05)"),
            1000, 0.5)
        self.assertEqual(state, "timeout")
        self.assertLess(len(data), 100)
        self.assertLess(time.monotonic() - started, 5)

    def test_a_missing_program_is_an_error_not_a_crash(self):
        self.assertEqual(backend.bounded_output(["/nonexistent/wl-paste"], 10, 1.0), (b"", "error"))

    def test_the_clipboard_is_only_read_through_the_bounded_reader(self):
        source = (ROOT / "backend.py").read_text()
        self.assertNotIn("capture_output=True, text=True, timeout=2).stdout\n    except", source.split("def latest_screenshot")[0])
        for line in source.splitlines():
            if '"wl-paste"' in line:
                self.assertNotIn("subprocess.", line, line)
        self.assertGreaterEqual(source.count("bounded_output(["), 2)

    def paste_with(self, data, state):
        seen = []
        real = backend.bounded_output
        backend.bounded_output = lambda argv, limit, wait: (seen.append((argv, limit)) or (data[:limit], state))
        try:
            return backend.handle({"cmd": "paste", "primary": bool(seen)}), seen
        finally:
            backend.bounded_output = real

    def test_paste_is_bounded_and_cleaned(self):
        out, seen = self.paste_with(("hi \x00\x1b[31m" + MARKUP + "\n" + "x" * 50000).encode(), "limit")
        self.assertTrue(out["ok"])
        self.assertLessEqual(len(out["text"]), backend.MAX_REPLY)
        self.assertNotIn("\x00", out["text"])
        self.assertNotIn("\x1b", out["text"])
        self.assertIn(MARKUP, out["text"])
        self.assertEqual(seen[0][1], backend.MAX_REPLY * 4)
        self.assertEqual(seen[0][0][0], "wl-paste")

    def test_paste_survives_a_character_cut_in_half(self):
        out, _ = self.paste_with("gr\u00fc\u00df".encode()[:-1], "limit")
        self.assertEqual(out, {"ok": True, "text": "gr\u00fc"})

    def test_paste_reports_an_owner_that_does_not_answer(self):
        out, _ = self.paste_with(b"partial", "timeout")
        self.assertFalse(out["ok"])
        self.assertNotIn("text", out)

    def test_primary_selection_is_asked_for_only_when_requested(self):
        seen = []
        real = backend.bounded_output
        backend.bounded_output = lambda argv, limit, wait: (seen.append(argv) or (b"", "eof"))
        try:
            backend.handle({"cmd": "paste"})
            backend.handle({"cmd": "paste", "primary": True})
            backend.handle({"cmd": "paste", "primary": "yes"})
        finally:
            backend.bounded_output = real
        self.assertEqual(["--primary" in argv for argv in seen], [False, True, False])


class TextBoxes(unittest.TestCase):
    """No text box may let Qt read the clipboard on the shell's GUI thread:
    no TextInput-based element at all, and the one writable TextEdit is
    TextEntry's, which takes paste away from Qt (tests/tst_textentry.qml)."""

    def code(self, qml):
        return "\n".join(line.split("//")[0] for line in qml.read_text().splitlines())

    def test_no_textinput_based_element(self):
        for qml in ROOT.glob("*.qml"):
            for name in ("TextField", "TextInput", "TextArea", "SpinBox", "ComboBox", "SearchField"):
                self.assertNotRegex(self.code(qml), r"\b%s\b" % name, "%s uses %s" % (qml.name, name))

    def test_the_only_writable_textedit_is_textentry(self):
        writable = []
        for qml in ROOT.glob("*.qml"):
            lines = self.code(qml).splitlines()
            for i, line in enumerate(lines):
                if not re.match(r"\s*TextEdit \{$", line):
                    continue
                depth, block = 0, []
                for inner in lines[i:]:
                    block.append(inner)
                    depth += inner.count("{") - inner.count("}")
                    if depth <= 0:
                        break
                if not re.search(r"^\s*readOnly: true$", "\n".join(block), re.M):
                    writable.append(qml.name)
        self.assertEqual(writable, ["TextEntry.qml"])

    def test_nothing_calls_qt_paste(self):
        for qml in ROOT.glob("*.qml"):
            code = self.code(qml)
            self.assertNotRegex(code, r"\.paste\(\)", qml.name)
            self.assertNotIn("canPaste", code, qml.name)

    def test_textentry_needs_nothing_but_qtquick(self):
        imports = [l for l in (ROOT / "TextEntry.qml").read_text().splitlines() if l.startswith("import ")]
        self.assertEqual(imports, ["import QtQuick"])


class InvisibleText(unittest.TestCase):
    """A name or label must not look empty, or like another one, by using
    characters that draw nothing."""

    def test_one_line_text_has_no_invisible_characters(self):
        self.assertEqual(backend.clean("Ma\u200bya\U000e0041", 50), "Maya")
        self.assertEqual(backend.clean("\u200b\u200b", 50), "")
        self.assertEqual(backend.clean("\u3164\u3164", 50), "")
        self.assertEqual(backend.clean("a\x85b\u2028c", 50), "a b c")

    def test_joiners_and_message_bodies_are_left_alone(self):
        family = "\U0001f468\u200d\U0001f467"
        self.assertEqual(backend.clean(family, 50), family)
        self.assertEqual(backend.clean("soft\xadhyphen", 50, multiline=True), "soft\xadhyphen")
        self.assertEqual(backend.clean("a\u2028b", 50, multiline=True), "a" + chr(10) + "b")

    def test_a_contact_label_is_cleaned_while_its_address_is_kept(self):
        raw = "tel:\u202a+1 555\u202c"
        real = backend.simple
        backend.simple = lambda payload, want: {"contacts": [{"name": "", "addresses": [raw, "email:a@example.org"]}]}
        try:
            out = backend.contacts("x")["contacts"]
        finally:
            backend.simple = real
        self.assertEqual(out[0]["address"], raw)
        self.assertEqual(out[0]["label"], "+1 555")
        self.assertEqual(out[0]["name"], "+1 555")
        self.assertEqual(out[1]["label"], "a@example.org")

    def test_the_panel_shows_labels_never_raw_keys(self):
        panel = (ROOT / "Panel.qml").read_text()
        self.assertNotIn("openThread.substring(4) :", panel)
        self.assertNotIn("String(name || thread", panel)
        self.assertNotIn("replace(/^(tel|email):/", panel)
        self.assertNotIn('thread.replace(/^tel:/', (ROOT / "BarWidget.qml").read_text())


class Screenshots(unittest.TestCase):
    """"Send latest screenshot" sends a screenshot: not the newest photo in
    Pictures, and not what a link points to."""

    def setUp(self):
        self.pictures = pathlib.Path(tempfile.mkdtemp())
        self.shots = self.pictures / "Screenshots"
        self.shots.mkdir()
        self.clock = 1_700_000_000

    def put(self, path, link=None):
        if link:
            path.symlink_to(link)
        else:
            path.write_bytes(b"x")
            self.clock += 10
            os.utime(path, (self.clock, self.clock))
        return str(path)

    def folders(self):
        return [(str(self.shots), True), (str(self.pictures), False)]

    def test_a_newer_photo_in_pictures_is_not_a_screenshot(self):
        shot = self.put(self.shots / "capture.png")
        self.put(self.pictures / "IMG_2041.jpg")
        self.put(self.pictures / "passport-scan.png")
        self.assertEqual(backend.latest_screenshot(self.folders()), shot)

    def test_pictures_itself_counts_only_for_screenshot_names(self):
        self.put(self.pictures / "holiday.png")
        old = self.put(self.pictures / "screenshot-2026-11-17_20-31-05.png")
        self.put(self.pictures / "notes.txt")
        self.assertEqual(backend.latest_screenshot(self.folders()), old)
        newer = self.put(self.pictures / "Screenshot from 2026-11-18.jpeg")
        self.assertEqual(backend.latest_screenshot(self.folders()), newer)

    def test_links_are_not_followed(self):
        secret = self.put(self.pictures / "private.png")
        self.put(self.shots / "screenshot-link.png", link=secret)
        self.put(self.pictures / "screenshot-other.png", link="/etc/hostname")
        self.assertIsNone(backend.latest_screenshot(self.folders()))

    def test_nothing_there(self):
        self.assertIsNone(backend.latest_screenshot([("/nonexistent/folder", True)]))
        self.assertIsNone(backend.latest_screenshot(self.folders()))

    def test_omarchys_own_folder_setting_wins(self):
        keep = {k: os.environ.pop(k, None) for k in ("OMASNAP_SCREENSHOT_DIR", "OMARCHY_SCREENSHOT_DIR")}
        try:
            os.environ["OMARCHY_SCREENSHOT_DIR"] = "/old/place"
            self.assertEqual(backend.screenshot_folders(), [("/old/place", True)])
            os.environ["OMASNAP_SCREENSHOT_DIR"] = "/new/place"
            self.assertEqual(backend.screenshot_folders(), [("/new/place", True)])
            os.environ["OMASNAP_SCREENSHOT_DIR"] = "relative/place"
            self.assertEqual(backend.screenshot_folders(), [("/old/place", True)])
        finally:
            for k, v in keep.items():
                os.environ.pop(k, None)
                if v is not None:
                    os.environ[k] = v

    def test_copied_files_must_be_plain_files(self):
        real = self.put(self.pictures / "report.pdf")
        self.put(self.pictures / "link.pdf", link=real)
        listing = ("file://%s" + chr(10) + "file://%s" + chr(10) + "file:///nonexistent/x") % (
            real, self.pictures / "link.pdf")
        answers = {"--list-types": b"text/uri-list", "--no-newline": listing.encode()}
        keep = backend.bounded_output
        backend.bounded_output = lambda argv, limit, wait: (answers[argv[1]], "eof")
        try:
            self.assertEqual(backend.clipboard_files(), [real])
        finally:
            backend.bounded_output = keep


class Pairing(unittest.TestCase):
    """Any device on the network can ask to pair, under any name. Nothing
    pairs on one key or one click, and nothing says who is asking."""

    def test_the_notification_claims_nothing_about_the_device(self):
        text = lambda **req: backend.notification_text(dict(kind="pairing", fingerprint="ab" * 16, **req))
        key, window, summary, body, icon, actions, urgency = text(name="Alex's iPhone")
        self.assertEqual(summary, "Pairing request")
        self.assertTrue(body.startswith('A device calling itself "Alex' + "'" + 's iPhone" asks to pair'))
        self.assertNotIn("iPhone wants", summary + body)
        self.assertEqual(actions, [])
        self.assertTrue(text()[3].startswith("A device asks to pair"))

    def test_requests_cannot_flood_notifications_or_lock_files(self):
        first = backend.notification_text({"kind": "pairing", "fingerprint": "ab" * 16})
        second = backend.notification_text({"kind": "pairing", "fingerprint": "cd" * 16})
        self.assertEqual(first[0], second[0])
        self.assertGreaterEqual(first[1], 30)

    def test_accepting_takes_two_separate_steps(self):
        panel = (ROOT / "Panel.qml").read_text()
        self.assertEqual(panel.count("tether.accept("), 1)
        confirm = panel.split("function confirmPairing(fingerprint) {")[1].split("}")[0]
        self.assertIn("tether.accept(fingerprint)", confirm)
        self.assertIn("pairingToConfirm !== String(fingerprint)", confirm)
        self.assertIn("tooSoon()", confirm)
        self.assertNotRegex(panel, r'k === "a" && root' + chr(92) + '.hasPending')
        self.assertIn('if (kind === "pending") togglePairing(data.fingerprint)', panel)
        self.assertNotIn("accept", (ROOT / "BarWidget.qml").read_text().split("ShellIpc {")[1].split("BarIconButton")[0])

    def test_a_wifi_pairing_is_not_renamed_after_the_bluetooth_phone(self):
        source = (ROOT / "backend.py").read_text()
        self.assertNotIn('["devices"][0]["name"] = result["phone_name"]', source)
        self.assertNotIn("viaBluetooth", (ROOT / "Panel.qml").read_text())

    def test_notifications_are_not_kept_in_history(self):
        self.assertIn('hints = {"transient": GLib.Variant("b", True)}', (ROOT / "backend.py").read_text())


class OtherFiles(unittest.TestCase):
    def test_setup_script(self):
        script = ROOT / "setup-bluetooth.sh"
        self.assertEqual(subprocess.run(["bash", "-n", str(script)]).returncode, 0)
        text = script.read_text()
        lines = [line.strip() for line in text.splitlines()]
        ask = lines.index('read -r -p "Continue? [y/N] " answer')
        changing = ("sudo ", "systemctl --user restart", "cat > ", "tether bt calls-enable", "mkdir ", "printf '[Service]")
        first = min(i for i, line in enumerate(lines) if line.startswith(changing))
        self.assertGreater(first, ask, "%r runs before the question" % lines[first])
        # Every file it writes is behind an "already there" check.
        self.assertLess(text.index('elif [[ -e $dropin ]]'), text.index('sudo tee "$dropin"'))
        self.assertLess(text.index('elif [[ -e $roles_file ]]'), text.index('cat > "$roles_file"'))
        answered_no = subprocess.run(["bash", str(script)], input="n" + chr(10), capture_output=True, text=True)
        if os.geteuid() != 0:
            self.assertEqual(answered_no.returncode, 0)
            self.assertTrue(answered_no.stdout.rstrip().endswith("Nothing changed."))

    def test_the_redirect_folder_holds_one_program(self):
        self.assertEqual(sorted(p.name for p in (ROOT / "bin").iterdir()), ["tether-gtk"])
        shim = (ROOT / "bin" / "tether-gtk").read_text()
        self.assertIn("exec /usr/bin/tether-gtk", shim)
        self.assertIn("[[:cntrl:]]", shim)

    def test_read_marks_follow_the_user_not_the_daemon(self):
        self.assertFalse(backend.handle.__code__.co_consts is None)
        service = (ROOT / "Service.qml").read_text()
        self.assertIn('{ cmd: "messages", thread: openThread, mark: _markRead }', service)
        self.assertEqual(service.count("_markRead = true"), 2)
        source = (ROOT / "backend.py").read_text()
        self.assertIn("if mark and unread:", source)
        self.assertIn('req.get("mark") is True', source)

    def test_an_error_reply_is_not_taken_for_a_status(self):
        self.assertIn('typeof s.installed !== "boolean"', (ROOT / "Service.qml").read_text())


if __name__ == "__main__":
    unittest.main()
