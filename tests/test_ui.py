"""Desktop interaction checks. Run with a display (e.g. DISPLAY=:99)."""
import os
from pathlib import Path
import sys
import tempfile
import tkinter as tk
import http.server
import gc
import threading
import time
import unittest
import weakref
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import App, AddDialog, EditDialog, Settings
from engine import Manager
from feature_dialogs import QueueDialog, CrawlDialog, FTPLoginDialog
import test_grabber as site_fixture


@unittest.skipUnless(os.name == "nt" or os.environ.get("DISPLAY"), "requires a desktop display")
class DesktopChecks(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.manager = Manager(Path(self.temp.name) / "state", autostart=False, use_proxy=False)
        self.root = tk.Tk()
        self.errors = []
        error_patch = patch("tkinter.messagebox.showerror", side_effect=lambda *args, **kwargs: self.errors.append(args))
        error_patch.start()
        self.addCleanup(error_patch.stop)
        self.root.report_callback_exception = lambda *exc: self.errors.append(exc)
        self.app = App(self.root, self.manager)
        self.a = self.manager.add("https://example.com/b.zip", start=False)
        self.b = self.manager.add("https://example.com/a.pdf", start=False)
        self.app.refresh_selection()
        self.root.update()

    def tearDown(self):
        self.assertFalse(self.errors, self.errors)
        self.app.closing = True
        self.root.after_cancel(self.app.refresh_timer)
        for child in self.root.winfo_children():
            if isinstance(child, tk.Toplevel):
                child.destroy()
        self.root.destroy()
        self.manager.shutdown()
        self.temp.cleanup()
        # Each test creates a fresh Tcl interpreter. Collect destroyed-widget
        # cycles on the UI thread before a later test starts a network worker.
        gc.collect()

    def test_sort_is_view_only_and_move_updates_real_queue(self):
        self.app.sort_by("name")
        self.assertEqual(self.app.table.get_children(), (self.b, self.a))
        self.assertEqual(list(self.manager.tasks), [self.a, self.b])
        self.app.table.selection_set(self.b)
        self.app.move(-1)
        self.assertEqual(self.app.table.get_children(), (self.b, self.a))
        self.assertEqual(list(self.manager.tasks), [self.b, self.a])

    def test_filtered_add_reveals_new_task(self):
        self.app.query.set("no-match")
        self.app.select_filter("已完成")
        dialog = AddDialog(self.app, "https://example.com/new.zip")
        dialog.submit(False)
        self.root.update()
        self.assertEqual(self.app.filter, "全部任务")
        self.assertEqual(self.app.query.get(), "")
        self.assertEqual(len(self.app.selected()), 1)
        self.assertEqual(self.app.selected()[0]["name"], "new.zip")
        self.assertEqual(self.app.heading.cget("text"), "全部任务")

    def test_edit_and_live_details_with_selection_removed(self):
        self.app.table.selection_set(self.a)
        self.app.details()
        self.assertFalse(self.app.actions["编辑 / 更新地址"].instate(["disabled"]))
        dialog = EditDialog(self.app, self.manager.snapshots()[0])
        dialog.variables["url"].set("https://example.com/renewed.zip")
        dialog.variables["limit_kib"].set("512")
        dialog.submit()
        self.assertEqual(self.manager.tasks[self.a].limit_kib, 512)
        self.app.details_popup()
        self.root.update()
        self.app.query.set("no-match")
        self.app.details()
        self.assertTrue(self.app.actions["编辑 / 更新地址"].instate(["disabled"]))
        self.app.draw_graph()
        self.assertTrue(self.app.graph.find_all())

    def test_dialog_controls_fit_at_minimum_window_size(self):
        self.root.geometry("1060x740")
        self.root.update()
        for dialog_type in (AddDialog, Settings, QueueDialog, CrawlDialog):
            dialog = dialog_type(self.app)
            self.root.update()
            self.assertLessEqual(dialog.body.winfo_reqheight(), dialog.winfo_height())
            dialog.destroy()
        self.assertLess(self.app.graph.winfo_rooty() + self.app.graph.winfo_height(), self.root.winfo_rooty() + self.root.winfo_height())
        self.assertTrue(self.app.footer.winfo_ismapped())
        self.assertLessEqual(self.app.footer.winfo_rooty() + self.app.footer.winfo_height(), self.root.winfo_rooty() + self.root.winfo_height())

    def test_queue_assignment_filter_and_independent_toggle(self):
        q = self.manager.create_queue("资源队列")
        self.app.table.selection_set(self.a)
        dialog = QueueDialog(self.app)
        dialog.table.selection_set(q)
        dialog.assign()
        self.root.update()
        self.assertEqual(self.manager.tasks[self.a].queue_id, q)
        self.assertEqual(self.app.active_queue, q)
        self.assertEqual(self.app.table.get_children(), (self.a,))
        self.app.queue()
        self.assertFalse(self.manager.queues[q]["enabled"])
        self.assertTrue(self.manager.settings["queue_enabled"])
        dialog.destroy()
        add = AddDialog(self.app, "ftp://example.com/file.zip")
        add.ftp_user.set("tester")
        add.ftp_password.set("ui-test-only-secret")
        add.submit(False)
        task = self.app.selected()[0]
        self.assertEqual(task["queue_id"], q)
        self.assertEqual(task["ftp_user"], "tester")
        self.assertNotIn("ui-test-only-secret", self.manager.state_file.read_text())
        login = FTPLoginDialog(self.app, task)
        login.password.set("new-test-secret")
        login.submit()
        self.assertEqual(self.manager.ftp_passwords[task["id"]], "new-test-secret")

    def test_site_discovery_preview_import_and_cancellation(self):
        site = http.server.ThreadingHTTPServer(("127.0.0.1", 0), site_fixture.Site)
        site.daemon_threads = True
        site.visited, site.robots_status = [], 200
        threading.Thread(target=site.serve_forever, daemon=True).start()
        try:
            q = self.manager.create_queue("站点资源")
            self.app.active_queue = q
            dialog = CrawlDialog(self.app)
            dialog.url.set(f"http://127.0.0.1:{site.server_port}/site/start.html")
            dialog.depth.set(0)
            dialog.extensions.set(".zip,.png")
            dialog.start()
            deadline = time.monotonic()+10
            while not dialog.stop_button.instate(["disabled"]) and time.monotonic() < deadline:
                self.root.update()
                time.sleep(0.01)
            self.assertTrue(dialog.stop_button.instate(["disabled"]))
            self.assertEqual(len(dialog.table.get_children()), 2)
            dialog.select_all()
            dialog.import_selected()
            imported = [t for t in self.manager.tasks.values() if t.queue_id == q]
            self.assertEqual(len(imported), 2)
            self.assertTrue(all(t.status == "paused" for t in imported))
            dialog.import_selected()
            self.assertEqual(len(self.manager.tasks), 4)
            dialog.start()
            dialog.destroy()
            dialog.worker.join(3)
            self.assertTrue(dialog.cancel.is_set())
            self.root.update()
        finally:
            site.shutdown()
            site.server_close()

    def test_closed_dialog_releases_tcl_variables_before_worker_gc(self):
        dialog = QueueDialog(self.app)
        name_ref, start_ref = weakref.ref(dialog.name), weakref.ref(dialog.start_at)
        dialog.destroy()
        self.assertIsNone(name_ref())
        self.assertIsNone(start_ref())
        thread = threading.Thread(target=gc.collect)
        thread.start()
        thread.join()
        self.root.update()


if __name__ == "__main__":
    unittest.main()
