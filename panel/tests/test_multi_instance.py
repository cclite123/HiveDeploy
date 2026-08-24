import os
import tempfile
import unittest
from types import SimpleNamespace

os.environ.setdefault("DB_PATH", os.path.join(tempfile.gettempdir(), f"hivedeploy-multi-{os.getpid()}.db"))

from app.instance_context import (
    assert_can_create, container_name, instance_key, instance_quota,
    storage_root,
)
from app.models import Instance
from app.progress_store import start_task, TaskAlreadyRunning, update_task


class MultiInstanceContextTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from app.database import Base, engine, SessionLocal
        from app.models import ProgressTask
        Base.metadata.create_all(bind=engine)
        db = SessionLocal(); db.query(ProgressTask).filter_by(username="multi-test").delete(); db.commit(); db.close()

    def test_default_context_keeps_legacy_names_and_paths(self):
        row = Instance(user_id=1, instance_key="default")
        self.assertEqual("astrbot_alice", container_name(row, "alice", "astrbot"))
        self.assertTrue(storage_root(row, "alice").replace("\\", "/").endswith("/instances/alice"))

    def test_non_default_context_is_isolated(self):
        row = Instance(user_id=1, instance_key="second")
        self.assertEqual("astrbot_alice_second", container_name(row, "alice", "astrbot"))
        self.assertTrue(storage_root(row, "alice").replace("\\", "/").endswith("/instances/alice/instances/second"))

    def test_quota_defaults_to_one_and_admin_is_enabled(self):
        user = SimpleNamespace(is_admin=False, multi_instance_enabled=False, max_instances=1)
        self.assertFalse(instance_quota(user, 1)["can_create"])
        with self.assertRaises(Exception):
            assert_can_create(user, 1)
        admin = SimpleNamespace(is_admin=True, multi_instance_enabled=False, max_instances=1)
        self.assertTrue(instance_quota(admin, 1)["can_create"])

    def test_tasks_are_isolated_by_instance(self):
        first = start_task("multi-test", "image_update", "both", "开始", instance_key="one")
        second = start_task("multi-test", "image_update", "both", "开始", instance_key="two")
        with self.assertRaises(TaskAlreadyRunning):
            start_task("multi-test", "image_update", "astrbot", "重复", instance_key="one")
        update_task(first["task_id"], "完成", done=True)
        update_task(second["task_id"], "完成", done=True)


if __name__ == "__main__":
    unittest.main()
