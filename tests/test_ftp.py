"""Real control/data socket integration against a small independent FTP origin."""
import hashlib
from pathlib import Path
import socket
import socketserver
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from engine import Manager, check_url

PAYLOAD = bytes(range(256)) * (12 * 1024)


class FTPOrigin(socketserver.StreamRequestHandler):
    def reply(self, message):
        self.wfile.write((message + "\r\n").encode())
        self.wfile.flush()

    def handle(self):
        passive, offset, user = None, 0, "anonymous"
        try:
            self.reply("220 FlowGet test FTP")
            while line := self.rfile.readline(65536):
                command, _, value = line.decode("utf-8").rstrip("\r\n").partition(" ")
                if command == "USER":
                    user = value
                    self.reply("331 Password required")
                elif command == "PASS":
                    self.reply("230 Logged in" if user == "anonymous" or (user == "tester" and value == "test-only-secret") else "530 Login incorrect")
                elif command == "TYPE":
                    self.reply("200 Binary mode")
                elif command == "SIZE":
                    if value in ("/missing", "/unknown"):
                        self.reply("550 No size available")
                    else:
                        self.reply("213 " + str(0 if value == "/empty" else len(PAYLOAD)))
                elif command == "MDTM":
                    if value in ("/no-mdtm", "/missing"):
                        self.reply("500 MDTM unavailable")
                    else:
                        self.reply("213 " + ("20261019020000" if self.server.changed else "20261019010000"))
                elif command == "REST":
                    if int(value) > 0 and self.server.reject_rest:
                        self.reply("502 REST unavailable")
                    else:
                        offset = int(value)
                        self.reply("350 Restart position accepted")
                elif command in ("PASV", "EPSV"):
                    if passive:
                        passive.close()
                    passive = socket.socket()
                    passive.bind(("127.0.0.1", 0))
                    passive.listen(1)
                    passive.settimeout(5)
                    port = passive.getsockname()[1]
                    self.reply(f"227 Passive (127,0,0,1,{port//256},{port%256})" if command == "PASV" else f"229 Extended Passive (|||{port}|)")
                elif command == "RETR":
                    if value == "/missing":
                        self.reply("550 File unavailable")
                        continue
                    payload = bytes(b ^ 255 for b in PAYLOAD) if self.server.changed else PAYLOAD
                    if value == "/empty":
                        payload = b""
                    self.server.retrieved.append((value, offset))
                    self.reply("150 Opening binary data connection")
                    stream, _ = passive.accept()
                    try:
                        for i in range(offset, len(payload), 32768):
                            stream.sendall(payload[i:i+32768])
                            if value == "/mutate":
                                self.server.changed = True
                            if value not in ("/fast", "/empty", "/unknown"):
                                time.sleep(0.01)
                    except OSError:
                        pass
                    finally:
                        stream.close()
                        passive.close()
                        passive, offset = None, 0
                    self.reply("226 Transfer complete")
                elif command == "QUIT":
                    self.reply("221 Goodbye")
                    break
                else:
                    self.reply("502 Unsupported test command")
        except (OSError, ValueError):
            pass
        finally:
            if passive:
                passive.close()


class FTPChecks(unittest.TestCase):
    def setUp(self):
        self.origin = socketserver.ThreadingTCPServer(("127.0.0.1", 0), FTPOrigin)
        self.origin.daemon_threads = True
        self.origin.changed, self.origin.reject_rest, self.origin.retrieved = False, False, []
        threading.Thread(target=self.origin.serve_forever, daemon=True).start()
        self.base = f"ftp://127.0.0.1:{self.origin.server_address[1]}"
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.manager = Manager(self.root / "state", use_proxy=False)

    def tearDown(self):
        self.assertTrue(self.manager.shutdown())
        self.origin.shutdown()
        self.origin.server_close()
        self.temp.cleanup()

    def add(self, path, **options):
        return self.manager.add(self.base+path, folder=self.root/"downloads", **options)

    def wait(self, task_id, status="complete"):
        deadline = time.monotonic()+15
        while time.monotonic() < deadline:
            task = self.manager.tasks[task_id]
            if task.status == status:
                return task
            if task.status == "error" and status != "error":
                self.fail(task.error)
            time.sleep(0.015)
        self.fail(str(self.manager.snapshots()))

    def assert_payload(self, task, payload=PAYLOAD):
        actual = (Path(task.folder)/task.name).read_bytes()
        self.assertEqual(actual, payload)
        self.assertEqual(task.sha256, hashlib.sha256(payload).hexdigest())

    def partial(self, route="/slow"):
        key = self.add(route)
        deadline = time.monotonic()+10
        while self.manager.tasks[key].done < 160000 and time.monotonic() < deadline:
            time.sleep(0.005)
        self.manager.pause(key)
        task = self.wait(key, "paused")
        self.assertGreater(task.done, 0)
        self.assertLess(task.done, len(PAYLOAD))
        return key

    def test_anonymous_single_connection_and_hash(self):
        task = self.wait(self.add("/fast", connections=32, expected_sha256=hashlib.sha256(PAYLOAD).hexdigest()))
        self.assert_payload(task)
        self.assertEqual(len(task.segments), 1)

    def test_rest_resume_after_restart(self):
        key = self.partial()
        self.assertTrue(self.manager.shutdown())
        self.manager = Manager(self.root/"state", use_proxy=False)
        self.manager.resume(key)
        self.assert_payload(self.wait(key))
        self.assertTrue(any(offset > 0 for route, offset in self.origin.retrieved))

    def test_missing_metadata_restarts(self):
        key = self.partial("/no-mdtm")
        self.manager.resume(key)
        task = self.wait(key)
        self.assertFalse(task.resumable)
        self.assert_payload(task)
        self.assertTrue(all(offset == 0 for route, offset in self.origin.retrieved))

    def test_rejected_rest_falls_back_to_full_transfer(self):
        key = self.partial()
        self.origin.reject_rest = True
        self.manager.resume(key)
        self.assert_payload(self.wait(key))
        self.assertEqual(self.origin.retrieved[-1][1], 0)

    def test_changed_remote_file_discards_old_bytes(self):
        key = self.partial()
        self.origin.changed = True
        self.manager.resume(key)
        self.assert_payload(self.wait(key), bytes(b ^ 255 for b in PAYLOAD))
        self.assertEqual(self.origin.retrieved[-1][1], 0)

    def test_mutation_during_transfer_is_not_published(self):
        task = self.wait(self.add("/mutate"), "error")
        self.assertIn("变化", task.error)
        self.assertFalse((Path(task.folder)/task.name).exists())

    def test_password_not_persisted_and_relogin_after_restart(self):
        key = self.add("/fast", start=False, ftp_user="tester", ftp_password="test-only-secret")
        self.assertNotIn("test-only-secret", self.manager.state_file.read_text())
        self.assertTrue(self.manager.shutdown())
        self.manager = Manager(self.root/"state", use_proxy=False)
        self.manager.resume(key)
        self.assertIn("密码", self.wait(key, "error").error)
        self.manager.set_ftp_login(key, "tester", "test-only-secret")
        self.manager.resume(key)
        self.assert_payload(self.wait(key))
        self.assertNotIn("test-only-secret", self.manager.state_file.read_text())

    def test_unknown_size_empty_and_missing(self):
        self.assert_payload(self.wait(self.add("/unknown")))
        self.assert_payload(self.wait(self.add("/empty")), b"")
        task = self.wait(self.add("/missing"), "error")
        self.assertIn("550", task.error)
        self.assertFalse((Path(task.folder)/task.name).exists())

    def test_bad_hash_and_invalid_ftp_commands(self):
        task = self.wait(self.add("/fast", expected_sha256="0"*64), "error")
        self.assertIn("SHA-256", task.error)
        self.assertFalse((Path(task.folder)/task.name).exists())
        for path in ("/x%0aDELE%20x", "/x%0d%0aQUIT", "/x%00", "/x?command=1"):
            with self.assertRaises(ValueError):
                self.add(path)
        with self.assertRaises(ValueError):
            self.add("/fast", ftp_user="anonymous\r\nQUIT")
        with self.assertRaises(ValueError):
            check_url(self.base+"/fast")  # browser bridge stays HTTP-only
