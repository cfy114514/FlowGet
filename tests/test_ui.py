"""Desktop interaction checks. Run with a display (e.g. DISPLAY=:99)."""
import os
from pathlib import Path
import sys
import tempfile
import tkinter as tk
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import App, AddDialog, EditDialog, Settings
from engine import Manager


@unittest.skipUnless(os.name == "nt" or os.environ.get("DISPLAY"), "requires a desktop display")
class DesktopChecks(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.manager = Manager(Path(self.temp.name) / "state", autostart=False)
        self.root = tk.Tk()
        self.errors = []
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
        for dialog_type in (AddDialog, Settings):
            dialog = dialog_type(self.app)
            self.root.update()
            self.assertLessEqual(dialog.body.winfo_reqheight(), dialog.winfo_height())
            dialog.destroy()
        self.assertLess(self.app.graph.winfo_rooty() + self.app.graph.winfo_height(), self.root.winfo_rooty() + self.root.winfo_height())


if __name__ == "__main__":
    unittest.main()
