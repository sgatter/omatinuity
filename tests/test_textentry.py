#!/usr/bin/env python3
"""Runs tests/tst_textentry.qml: the panel's text boxes type, take Enter and
Escape, and never let Qt paste. Needs Qt's qmltestrunner (qt6-declarative);
it draws nothing, the offscreen platform has no window on any screen.

Run from the plugin folder:  python3 -m unittest discover -s tests"""
import os
import pathlib
import shutil
import subprocess
import unittest

TESTS = pathlib.Path(__file__).resolve().parent
RUNNER = next((p for p in ("/usr/lib/qt6/bin/qmltestrunner", shutil.which("qmltestrunner6"),
                           shutil.which("qmltestrunner")) if p and os.access(p, os.X_OK)), None)


@unittest.skipUnless(RUNNER, "needs qmltestrunner from qt6-declarative")
class TextEntry(unittest.TestCase):
    def test_in_qt(self):
        env = dict(os.environ, QT_QPA_PLATFORM="offscreen", QT_QUICK_BACKEND="software")
        r = subprocess.run([RUNNER, "-input", str(TESTS / "tst_textentry.qml")],
                           capture_output=True, text=True, timeout=120, env=env)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertRegex(r.stdout, r"Totals: \d+ passed, 0 failed, 0 skipped")
        self.assertIn("PASS   : qmltestrunner::TextEntry::test_paste_keys_never_paste_natively()", r.stdout)


if __name__ == "__main__":
    unittest.main()
