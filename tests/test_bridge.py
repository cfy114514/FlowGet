import io
import json
import os
from pathlib import Path
import struct
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from native_host import accept_message, read_message, write_message
from engine import Manager

ORIGIN = "chrome-extension://" + "a"*32 + "/"


class BridgeChecks(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / "bridge.json").write_text(json.dumps({"allowed_origins": [ORIGIN]}), encoding="utf-8")

    def tearDown(self):
        self.temp.cleanup()

    def test_unicode_byte_framing(self):
        value = {"action": "add", "url": "https://example.com/文档"}
        stream = io.BytesIO()
        write_message(stream, value)
        body = stream.getvalue()
        self.assertEqual(struct.unpack("<I", body[:4])[0], len(body)-4)
        self.assertEqual(read_message(io.BytesIO(body)), value)

    def test_bad_frames(self):
        for body in (b"", b"\x01", struct.pack("<I", 70000), struct.pack("<I", 9)+b"{}"):
            with self.assertRaises(ValueError):
                read_message(io.BytesIO(body))

    def test_unknown_extension_denied(self):
        for origin in ("", "https://example.com/", "chrome-extension://"+"b"*32+"/"):
            with self.assertRaises(ValueError):
                accept_message(origin, {"action": "add", "url": "https://example.com/a.zip"}, self.root)
        self.assertFalse((self.root / "inbox").exists())

    def test_non_http_and_embedded_credentials_denied(self):
        for url in ("file:///C:/test", "javascript:alert(1)", "http://user:pass@example.com/file"):
            with self.assertRaises(ValueError):
                accept_message(ORIGIN, {"action": "add", "url": url}, self.root)

    def test_caller_fields_cannot_set_destination(self):
        task_id = accept_message(ORIGIN, {"action": "add", "url": "https://example.com/a.zip", "folder": "C:/Windows", "connections": 1000}, self.root)
        value = json.loads((self.root / "inbox" / f"{task_id}.json").read_text())
        self.assertEqual(set(value), {"url", "name"})

    def test_import_once_and_replay_deduplication(self):
        task_id = accept_message(ORIGIN.rstrip("/"), {"action": "add", "url": "https://example.com/a.zip"}, self.root)
        path = self.root / "inbox" / f"{task_id}.json"
        original = path.read_text()
        manager = Manager(self.root, autostart=False)
        manager.configure(queue_enabled=False)
        try:
            manager._consume_inbox()
            self.assertEqual(len(manager.tasks), 1)
            self.assertFalse(path.exists())
            path.write_text(original)
            manager._consume_inbox()
            self.assertEqual(len(manager.tasks), 1)
            reloaded = Manager(self.root, autostart=False)
            self.assertEqual(len(reloaded.tasks), 1)
            reloaded.shutdown()
        finally:
            manager.shutdown()

    def test_invalid_inbox_is_quarantined(self):
        inbox = self.root / "inbox"
        inbox.mkdir()
        (inbox / "bad-name.json").write_text('{}')
        manager = Manager(self.root, autostart=False)
        try:
            manager._consume_inbox()
            self.assertFalse(manager.tasks)
            self.assertTrue((inbox / "bad-name.rejected").exists())
        finally:
            manager.shutdown()


if __name__ == "__main__":
    unittest.main(verbosity=2)
