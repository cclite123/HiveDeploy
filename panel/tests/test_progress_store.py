import importlib
import os
import tempfile
import unittest


class ProgressStoreTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        os.environ["DB_PATH"] = os.path.join(cls.tmp.name, "progress.db")
        from app import database, models
        models.Base.metadata.create_all(bind=database.engine)
        cls.store = importlib.import_module("app.progress_store")

    @classmethod
    def tearDownClass(cls):
        from app.database import engine
        engine.dispose()
        cls.tmp.cleanup()

    def setUp(self):
        from app.database import SessionLocal
        from app.models import ProgressTask
        db = SessionLocal()
        db.query(ProgressTask).delete()
        db.commit()
        db.close()

    def test_progress_survives_a_fresh_database_session(self):
        created = self.store.start_task("alice", "image_update", "astrbot", "开始下载")
        self.store.update_task(created["task_id"], "下载镜像层", "42%")

        refreshed_page = self.store.get_current_task("alice")

        self.assertEqual(created["task_id"], refreshed_page["task_id"])
        self.assertEqual("下载镜像层", refreshed_page["step"])
        self.assertEqual("42%", refreshed_page["detail"])
        self.assertTrue(refreshed_page["running"])

    def test_concurrent_task_for_same_user_is_rejected(self):
        self.store.start_task("alice", "image_update", "both", "开始")
        with self.assertRaises(self.store.TaskAlreadyRunning):
            self.store.start_task("alice", "image_update", "napcat", "重复")

    def test_users_are_isolated(self):
        first = self.store.start_task("alice", "image_update", "both", "A")
        second = self.store.start_task("bob", "image_update", "both", "B")
        self.assertEqual(first["task_id"], self.store.get_current_task("alice")["task_id"])
        self.assertEqual(second["task_id"], self.store.get_current_task("bob")["task_id"])

    def test_restart_marks_running_tasks_interrupted(self):
        self.store.start_task("alice", "image_update", "both", "下载中")
        self.assertEqual(1, self.store.interrupt_running_tasks())
        task = self.store.get_current_task("alice")
        self.assertEqual("interrupted", task["status"])
        self.assertFalse(task["running"])

    def test_success_and_failure_are_persisted_as_terminal_states(self):
        success = self.store.start_task("alice", "image_update", "both", "开始")
        self.store.update_task(success["task_id"], "更新完成", done=True)
        completed = self.store.get_current_task("alice")
        self.assertEqual("success", completed["status"])
        self.assertTrue(completed["done"])

        failed = self.store.start_task("alice", "image_update", "astrbot", "开始")
        self.store.update_task(failed["task_id"], "更新失败", error="network timeout")
        current = self.store.get_current_task("alice")
        self.assertEqual("failed", current["status"])
        self.assertEqual("network timeout", current["error"])
        self.assertFalse(current["running"])

    def test_dashboard_queries_current_task_after_page_load(self):
        template = os.path.join(
            os.path.dirname(__file__), "..", "templates", "dashboard.html"
        )
        with open(template, encoding="utf-8") as handle:
            source = handle.read()
        self.assertIn("/api/instance/update_progress/current", source)
        self.assertIn("resumeActiveTask();", source)


if __name__ == "__main__":
    unittest.main()
