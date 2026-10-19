"""Multi-queue migration, fair dispatch, isolated controls and schedules."""
import json
from pathlib import Path
import sys
import threading
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from engine import Manager, ACTIVE
import test_engine as http_fixture


class QueueChecks(unittest.TestCase):
    setUpClass = classmethod(http_fixture.EngineChecks.setUpClass.__func__)
    tearDownClass = classmethod(http_fixture.EngineChecks.tearDownClass.__func__)
    setUp = http_fixture.EngineChecks.setUp
    tearDown = http_fixture.EngineChecks.tearDown
    add = http_fixture.EngineChecks.add
    wait = http_fixture.EngineChecks.wait
    assert_file = http_fixture.EngineChecks.assert_file

    def test_independent_pause_and_concurrency_caps(self):
        self.manager.configure(queue_enabled=False, parallel=3)
        q = self.manager.create_queue("资料", parallel=1)
        self.manager.configure_queue("default", name="默认队列", parallel=1)
        a = self.add("/slow", connections=1)
        b = self.add("/slow", connections=1, queue_id=q)
        c = self.add("/slow", connections=1, queue_id=q)
        self.manager.set_queue(True)
        deadline = time.monotonic()+10
        while time.monotonic() < deadline and not all(self.manager.tasks[key].done > 65536 for key in (a, b)):
            time.sleep(0.01)
        with self.manager.lock:
            self.assertEqual(len(self.manager.running), 2)
            self.assertEqual(self.manager.tasks[c].status, "queued")
        self.manager.set_queue(False, "default")
        self.wait(a, "paused")
        self.assert_file(self.wait(b))
        self.assert_file(self.wait(c))
        self.assertTrue(self.manager.settings["queue_enabled"])
        self.assertTrue(self.manager.queues[q]["enabled"])

    def test_round_robin_avoids_queue_starvation(self):
        self.manager.configure(queue_enabled=False, parallel=1)
        q = self.manager.create_queue("第二队列")
        a, b = self.add("/slow", connections=1), self.add("/range")
        c = self.add("/range", queue_id=q)
        self.manager.set_queue(True)
        self.assert_file(self.wait(a))
        self.assert_file(self.wait(c))
        self.assert_file(self.wait(b))
        self.assertLess(self.manager.tasks[c].completed, self.manager.tasks[b].completed)

    def test_queue_schedule_stops_only_its_own_tasks(self):
        self.manager.configure(parallel=2)
        q = self.manager.create_queue("夜间下载", parallel=1)
        self.manager.configure_queue(q, name="夜间下载", parallel=1, schedule_at=time.time()+0.7, stop_at=time.time()+1.7)
        a = self.add("/slow", connections=1, queue_id=q)
        b = self.add("/range")
        time.sleep(0.25)
        self.assertEqual(self.manager.tasks[a].done, 0)
        self.assert_file(self.wait(b))
        task = self.wait(a, "paused")
        self.assertGreater(task.done, 0)
        self.assertFalse(self.manager.queues[q]["enabled"])
        self.assertTrue(self.manager.settings["queue_enabled"])
        self.assertTrue(self.manager.shutdown())
        self.manager = Manager(self.root/"state", use_proxy=False)
        self.assertEqual(self.manager.tasks[a].queue_id, q)
        self.assertFalse(self.manager.queues[q]["enabled"])
        self.assertEqual(self.manager.queues[q]["stop_at"], "")

    def test_migration_from_version_one(self):
        self.manager.configure(queue_enabled=False)
        a = self.add("/range", start=False)
        self.assertTrue(self.manager.shutdown())
        state = json.loads(self.manager.state_file.read_text())
        state["version"] = 1
        state.pop("queues")
        for task in state["tasks"]:
            task.pop("queue_id")
            task.pop("ftp_user")
        self.manager.state_file.write_text(json.dumps(state))
        self.manager = Manager(self.root/"state", use_proxy=False)
        self.assertEqual(self.manager.tasks[a].queue_id, "default")
        self.assertEqual(self.manager.tasks[a].status, "paused")
        self.assertFalse(self.manager.settings["queue_enabled"])

    def test_assignment_is_atomic_and_deletion_preserves_tasks(self):
        self.manager.configure(queue_enabled=False)
        a, b = self.add("/range"), self.add("/range")
        q = self.manager.create_queue("新队列")
        self.manager.tasks[b].status = "downloading"
        with self.assertRaises(ValueError):
            self.manager.assign_queue([a, b], q)
        self.assertEqual(self.manager.tasks[a].queue_id, "default")
        self.manager.tasks[b].status = "paused"
        self.manager.assign_queue([a, b], q)
        with self.assertRaises(ValueError):
            self.manager.delete_queue(q)
        self.manager.assign_queue([a, b], "default")
        self.manager.delete_queue(q)
        self.assertEqual(len(self.manager.tasks), 2)
        with self.assertRaises(ValueError):
            self.manager.delete_queue("default")

    def test_move_in_queue_leaves_other_queue_positions_unchanged(self):
        self.manager.configure(queue_enabled=False)
        q = self.manager.create_queue("其他")
        a, b, c = self.add("/range"), self.add("/range", queue_id=q), self.add("/range")
        self.manager.move([c], -1, queue_id="default")
        self.assertEqual(list(self.manager.tasks), [c, b, a])
        self.assertEqual(self.manager.tasks[b].queue_id, q)

    def test_batch_import_is_paused_validated_and_deduplicated(self):
        self.manager.configure(queue_enabled=False)
        q = self.manager.create_queue("抓取结果")
        urls = [self.base+"/range", self.base+"/range", self.base+"/slow"]
        keys = self.manager.add_batch(urls, queue_id=q, folder=self.root/"downloads")
        self.assertEqual(len(keys), 2)
        self.assertTrue(all(t.status == "paused" and t.queue_id == q for t in self.manager.tasks.values()))
        self.assertEqual(self.manager.add_batch(urls, queue_id=q), [])
        with self.assertRaises(ValueError):
            self.manager.add_batch([self.base+"/new", "file:///outside"], queue_id=q)
        self.assertEqual(len(self.manager.tasks), 2)
        self.manager.set_queue(True, q)
        self.manager.set_queue(True)
        self.manager.resume(keys[0])
        self.assert_file(self.wait(keys[0]))

    def test_invalid_queue_configuration_does_not_mutate(self):
        q = self.manager.create_queue("资料")
        for name, parallel in (("", 2), ("资料", 0), ("默认队列", 2)):
            with self.assertRaises(ValueError):
                self.manager.configure_queue(q, name=name, parallel=parallel)
        with self.assertRaises(ValueError):
            self.manager.configure_queue(q, name="资料", parallel=2, schedule_at=time.time()-1)
        self.assertEqual(self.manager.queues[q]["name"], "资料")

    def test_stop_during_worker_cleanup_keeps_completed_file_state(self):
        published, release = threading.Event(), threading.Event()
        assemble = self.manager._assemble
        def hold_cleanup(*args):
            assemble(*args)
            published.set()
            release.wait(5)
        self.manager._assemble = hold_cleanup
        key = self.add("/range")
        try:
            self.assertTrue(published.wait(10))
            self.assertIn(key, self.manager.running)
            self.manager.pause(key)
            self.assertEqual(self.manager.tasks[key].status, "complete")
            self.manager.set_queue(False, "default")
            self.assertEqual(self.manager.tasks[key].status, "complete")
            self.manager.set_queue(False)
            self.assertEqual(self.manager.tasks[key].status, "complete")
            self.assert_file(self.manager.tasks[key])
        finally:
            release.set()
