"""Black-box HTTP boundary checks against a controlled local origin."""
import hashlib
import http.server
import importlib.util
import json
from pathlib import Path
import re
import socket
import sys
import tempfile
import threading
import time
import unittest
import os

SOURCE = Path(__file__).resolve().parents[1]
if not (SOURCE / "engine.py").exists():
    SOURCE = Path(__file__).resolve().parents[2] / "outputs" / "FlowGet"
sys.path.insert(0, str(SOURCE))
from engine import Manager, clean_name, check_url

PAYLOAD = bytes(range(256)) * (12 * 1024)
DIGEST = hashlib.sha256(PAYLOAD).hexdigest()


class Origin(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    counts = {}
    ranges = []
    lock = threading.Lock()
    changed = False

    def log_message(self, *_):
        pass

    def handle(self):
        try:
            super().handle()
        except (ConnectionError, OSError):
            # Probe cancellation and deliberate broken-connection cases are
            # expected; their client-side outcomes are asserted by the tests.
            pass

    def do_GET(self):
        route = self.path.split("?")[0]
        with self.lock:
            self.counts[route] = self.counts.get(route, 0) + 1
            ordinal = self.counts[route]
        if route == "/redirect":
            self.send_response(302)
            self.send_header("Location", "/range")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if route == "/missing":
            self.send_error(404)
            return
        payload = PAYLOAD
        if route == "/empty":
            payload = b""
        if route == "/change" and self.changed:
            payload = bytes(b ^ 255 for b in PAYLOAD)
        etag = '"v2"' if route == "/change" and self.changed else '"v1"'
        ranged = self.headers.get("Range")
        start, end = 0, len(payload) - 1
        honor = bool(ranged and route not in ("/ignore", "/unknown"))
        if honor:
            start, end = map(int, re.fullmatch(r"bytes=(\d+)-(\d+)", ranged).groups())
            if not payload:
                self.send_response(416)
                self.send_header("Content-Range", "bytes */0")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            self.ranges.append((route, start, end, self.headers.get("If-Range")))
        body = payload[start:end+1]
        self.send_response(206 if honor else 200)
        if route != "/noetag":
            self.send_header("ETag", etag)
        if route == "/disposition":
            self.send_header("Content-Disposition", "attachment; filename*=UTF-8''%E6%96%87%E6%A1%A3.bin")
        if honor:
            actual_start = start+1 if route == "/bad-range" and end > 0 else start
            self.send_header("Content-Range", f"bytes {actual_start}-{end}/{len(payload)}")
        if route != "/unknown":
            self.send_header("Content-Length", str(len(body)))
        else:
            self.send_header("Connection", "close")
            self.close_connection = True
        self.end_headers()
        try:
            if route == "/flaky" and ordinal == 2:
                self.wfile.write(body[:min(128*1024, len(body))])
                self.wfile.flush()
                self.connection.shutdown(socket.SHUT_RDWR)
                self.connection.close()
                return
            for index in range(0, len(body), 32768):
                self.wfile.write(body[index:index+32768])
                self.wfile.flush()
                if route in ("/slow", "/change", "/limit") and end > 0:
                    time.sleep(0.02)
        except (OSError, ConnectionError):
            pass


class EngineChecks(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Origin)
        cls.server.daemon_threads = True
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.manager = Manager(self.root / "state", use_proxy=False, backend=os.environ.get("FLOWGET_TEST_BACKEND", "auto"))

    def tearDown(self):
        self.assertTrue(self.manager.shutdown())
        self.temp.cleanup()

    def add(self, route, **kwargs):
        return self.manager.add(self.base+route, folder=self.root / "downloads", name="sample.bin", **kwargs)

    def wait(self, task_id, state="complete", timeout=20):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            t = self.manager.tasks[task_id]
            if t.status == state:
                return t
            if t.status == "error" and state != "error":
                self.fail(t.error)
            time.sleep(0.025)
        self.fail(f"timeout: {self.manager.snapshots()}")

    def assert_file(self, t, expected=DIGEST):
        content = (Path(t.folder) / t.name).read_bytes()
        self.assertEqual(hashlib.sha256(content).hexdigest(), expected)
        self.assertEqual(t.sha256, expected)

    def test_parallel_hash(self):
        t = self.wait(self.add("/range", connections=6, expected_sha256=DIGEST))
        self.assertGreater(len(t.segments), 1)
        self.assert_file(t)

    def test_ignore_ranges(self):
        t = self.wait(self.add("/ignore"))
        self.assertFalse(t.resumable)
        self.assertEqual(len(t.segments), 1)
        self.assert_file(t)

    def test_unknown_length(self):
        self.assert_file(self.wait(self.add("/unknown")))

    def test_no_validator(self):
        t = self.wait(self.add("/noetag"))
        self.assertFalse(t.resumable)
        self.assertEqual(len(t.segments), 1)
        self.assert_file(t)

    def test_empty(self):
        t = self.wait(self.add("/empty"))
        self.assert_file(t, hashlib.sha256(b"").hexdigest())

    def test_redirect(self):
        self.assert_file(self.wait(self.add("/redirect")))

    def test_disconnect_retry(self):
        self.assert_file(self.wait(self.add("/flaky")))

    def test_bad_range_never_published(self):
        t = self.wait(self.add("/bad-range"), "error")
        self.assertFalse((Path(t.folder) / t.name).exists())
        self.assertIn("范围", t.error)

    def test_bad_digest_never_published(self):
        t = self.wait(self.add("/range", expected_sha256="0"*64), "error")
        self.assertIn("SHA-256", t.error)
        self.assertFalse((Path(t.folder) / t.name).exists())

    def test_missing(self):
        t = self.wait(self.add("/missing"), "error")
        self.assertIn("404", t.error)

    def test_collision_preserves_existing(self):
        target = self.root / "downloads" / "sample.bin"
        target.parent.mkdir()
        target.write_bytes(b"keep")
        t = self.wait(self.add("/range"))
        self.assertNotEqual(t.name, "sample.bin")
        self.assertEqual(target.read_bytes(), b"keep")
        self.assert_file(t)

    def pause_somewhere(self, route):
        task_id = self.add(route, connections=2)
        until = time.monotonic() + 10
        while self.manager.tasks[task_id].done < 160000 and time.monotonic() < until:
            time.sleep(0.01)
        self.manager.pause(task_id)
        t = self.wait(task_id, "paused")
        self.assertGreater(t.done, 0)
        self.assertLess(t.done, t.total)
        return task_id, t.done

    def test_pause_resume_across_manager_restart(self):
        task_id, downloaded = self.pause_somewhere("/slow")
        self.assertTrue(self.manager.shutdown())
        self.manager = Manager(self.root / "state", use_proxy=False, backend=os.environ.get("FLOWGET_TEST_BACKEND", "auto"))
        self.assertEqual(self.manager.tasks[task_id].status, "paused")
        self.assertEqual(self.manager.tasks[task_id].done, downloaded)
        before = len(Origin.ranges)
        self.manager.resume(task_id)
        self.assert_file(self.wait(task_id))
        requests = Origin.ranges[before:]
        self.assertTrue(any(start > 0 and tag == '"v1"' for route, start, end, tag in requests))

    def test_changed_representation_discards_old_parts(self):
        Origin.changed = False
        task_id, downloaded = self.pause_somewhere("/change")
        Origin.changed = True
        self.manager.resume(task_id)
        t = self.wait(task_id)
        changed = hashlib.sha256(bytes(b ^ 255 for b in PAYLOAD)).hexdigest()
        self.assert_file(t, changed)
        Origin.changed = False

    def test_queue_parallel_cap(self):
        self.manager.configure(parallel=1, queue_enabled=False)
        a, b = self.add("/slow"), self.add("/slow")
        time.sleep(0.5)
        self.assertEqual(self.manager.tasks[a].done, 0)
        self.manager.configure(queue_enabled=True)
        until = time.monotonic() + 10
        while self.manager.tasks[a].done < 65536 and time.monotonic() < until:
            time.sleep(0.02)
        self.assertEqual(self.manager.tasks[b].status, "queued")
        self.assert_file(self.wait(a))
        self.assert_file(self.wait(b))

    def test_scheduled_queue(self):
        self.manager.configure(queue_enabled=False, schedule_at=time.time()+0.8)
        task_id = self.add("/range")
        time.sleep(0.3)
        self.assertEqual(self.manager.tasks[task_id].done, 0)
        self.assert_file(self.wait(task_id))

    def test_speed_limit(self):
        self.manager.configure(limit_kib=1024)
        started = time.monotonic()
        self.wait(self.add("/limit", connections=4))
        self.assertGreater(time.monotonic() - started, 2.5)

    def test_filename_url_safety(self):
        self.assertEqual(clean_name("CON.exe"), "_CON.exe")
        self.assertNotIn("/", clean_name("../../outside.bin"))
        for url in ("file:///C:/test", "https://user:pass@example.com/x", "http://host/\nabc"):
            with self.assertRaises(ValueError):
                check_url(url)

    def test_server_filename_and_explicit_name(self):
        automatic = self.manager.add(self.base+"/disposition", folder=self.root/"downloads")
        t = self.wait(automatic)
        self.assertEqual(t.name, "文档.bin")
        self.assert_file(t)
        explicit = self.manager.add(self.base+"/disposition", folder=self.root/"downloads", name="chosen.bin")
        t = self.wait(explicit)
        self.assertEqual(t.name, "chosen.bin")
        self.assert_file(t)


if __name__ == "__main__":
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(EngineChecks)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    output = {"tests": result.testsRun, "failures": len(result.failures), "errors": len(result.errors),
        "success": result.wasSuccessful(), "server": "127.0.0.1 controlled HTTP origin", "payload_sha256": DIGEST}
    (Path(__file__).parent / "results.json").write_text(json.dumps(output, indent=2), encoding="utf-8")
    sys.exit(not result.wasSuccessful())
