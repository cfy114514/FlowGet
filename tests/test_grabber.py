"""A controlled website verifies discovery scope, robots, budgets and import."""
import http.server
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from engine import Manager
from grabber import Crawler

ASSET = b"FlowGet site grabber integration\n" * 1024


class Site(http.server.BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_GET(self):
        self.server.visited.append(self.path)
        path = self.path.split("?")[0]
        if path == "/robots.txt":
            self.send_response(self.server.robots_status)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"User-agent: *\nDisallow: /site/private/\n")
            return
        if path in ("/site/jump", "/site/robotjump", "/site/loop"):
            location = {"/site/jump": "/outside/secret.pdf", "/site/robotjump": "/site/private/blocked.pdf", "/site/loop": "/site/start.html"}[path]
            self.send_response(302)
            self.send_header("Location", location)
            self.end_headers()
            return
        pages = {
            "/site/start.html": f'''<base href="/site/">
                <a href="archive.zip#one">zip</a><a href="archive.zip#two">duplicate</a>
                <a href="next.html">next</a><img src="image.png">
                <a href="/outside/secret.pdf">outside</a>
                <a href="http://localhost:{self.server.server_port}/site/foreign.pdf">foreign host</a>
                <a href="private/blocked.pdf">robots</a>
                <a href="jump">outside redirect</a><a href="robotjump">robots redirect</a>
                <a href="loop">cycle</a><a href="mailto:test@example.com">email</a>''',
            "/site/next.html": '<a href="deep.html">deep</a><a href="file2.pdf?x=1&amp;y=2">pdf</a>',
            "/site/deep.html": '<a href="deep-file.zip">deeper zip</a>',
            "/site/large.html": "<html>" + "x" * (2*1024**2),
        }
        body = pages[path].encode() if path in pages else ASSET
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8" if path in pages else "application/pdf" if path.endswith(".pdf") else "application/octet-stream")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (ConnectionError, OSError):
            pass


class GrabberChecks(unittest.TestCase):
    def setUp(self):
        self.site = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Site)
        self.site.daemon_threads = True
        self.site.visited, self.site.robots_status = [], 200
        threading.Thread(target=self.site.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.site.server_port}"

    def tearDown(self):
        self.site.shutdown()
        self.site.server_close()

    def crawl(self, **options):
        return Crawler(self.base+"/site/start.html", use_proxy=False, delay=0, **options).run()

    def test_filters_dedup_depth_scope_and_redirects(self):
        report = self.crawl(depth=1, extensions=".zip,.pdf")
        urls = {item["url"] for item in report["items"]}
        self.assertEqual(urls, {self.base+"/site/archive.zip", self.base+"/site/file2.pdf?x=1&y=2"})
        self.assertNotIn("/site/deep.html", self.site.visited)
        self.assertNotIn("/outside/secret.pdf", self.site.visited)
        self.assertNotIn("/site/private/blocked.pdf", self.site.visited)
        self.assertFalse(any("foreign.pdf" in path for path in self.site.visited))
        self.assertTrue(any("范围" in error for error in report["errors"]))
        self.assertTrue(any("robots" in error for error in report["errors"]))

    def test_depth_zero_and_page_budget(self):
        report = self.crawl(depth=0, extensions=".zip")
        self.assertEqual([x["url"] for x in report["items"]], [self.base+"/site/archive.zip"])
        self.assertEqual(report["pages"], 1)
        report = self.crawl(depth=3, max_pages=1)
        self.assertTrue(report["truncated"])
        self.assertEqual(report["pages"], 1)

    def test_deeper_discovery_and_full_site_scope(self):
        report = self.crawl(depth=2, same_directory=False, extensions=".zip,.pdf")
        urls = {x["url"] for x in report["items"]}
        self.assertIn(self.base+"/site/deep-file.zip", urls)
        self.assertIn(self.base+"/outside/secret.pdf", urls)
        self.assertNotIn(self.base+"/site/private/blocked.pdf", urls)

    def test_robots_failure_and_access_denial(self):
        self.site.robots_status = 500
        report = self.crawl()
        self.assertEqual(report["pages"], 0)
        self.assertTrue(report["errors"])
        self.site.robots_status = 403
        self.site.visited.clear()
        report = self.crawl()
        self.assertEqual(report["pages"], 0)
        self.assertEqual(self.site.visited, ["/robots.txt"])

    def test_cancellation_and_large_page_budget(self):
        stop = threading.Event()
        crawler = Crawler(self.base+"/site/start.html", use_proxy=False, delay=0)
        report = crawler.run(stop, lambda _: stop.set())
        self.assertTrue(report["cancelled"])
        self.assertEqual(report["pages"], 1)
        report = Crawler(self.base+"/site/large.html", use_proxy=False, delay=0).run()
        self.assertTrue(report["truncated"])
        self.assertTrue(report["errors"])

    def test_scope_rejects_encoded_traversal_and_ports(self):
        crawler = Crawler(self.base+"/site/start.html")
        for suffix in ("/site/%2e%2e/outside/secret.pdf", "/site-other/file.pdf"):
            self.assertFalse(crawler.in_scope(self.base+suffix))
        self.assertFalse(crawler.in_scope("http://127.0.0.1:1/site/file.pdf"))
        with self.assertRaises(ValueError):
            Crawler("ftp://example.com/file")

    def test_discovered_resources_download_into_selected_queue(self):
        report = self.crawl(depth=0, extensions=".zip")
        with tempfile.TemporaryDirectory() as directory:
            manager = Manager(Path(directory)/"state", use_proxy=False)
            try:
                q = manager.create_queue("网站资源")
                keys = manager.add_batch([x["url"] for x in report["items"]], queue_id=q, folder=Path(directory)/"downloads")
                self.assertEqual(len(keys), 1)
                self.assertEqual(manager.tasks[keys[0]].status, "paused")
                manager.resume(keys[0])
                deadline = time.monotonic()+10
                while manager.tasks[keys[0]].status not in {"complete", "error"} and time.monotonic() < deadline:
                    time.sleep(0.02)
                task = manager.tasks[keys[0]]
                self.assertEqual(task.status, "complete", task.error)
                self.assertEqual(task.queue_id, q)
                self.assertEqual((Path(task.folder)/task.name).read_bytes(), ASSET)
            finally:
                self.assertTrue(manager.shutdown())
