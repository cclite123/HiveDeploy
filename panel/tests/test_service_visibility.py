import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

from fastapi import HTTPException
from jinja2 import Environment, FileSystemLoader


PANEL_DIR = Path(__file__).resolve().parents[1]
if str(PANEL_DIR) not in sys.path:
    sys.path.insert(0, str(PANEL_DIR))

from app.service_access import available_instance_services, ensure_instance_service


def _user(bot_type=None):
    instance = None if bot_type is None else SimpleNamespace(bot_type=bot_type)
    return SimpleNamespace(instance=instance, is_admin=False)


class ServiceAccessTests(unittest.TestCase):
    def test_only_astrbot_and_current_bot_are_available(self):
        self.assertEqual(("astrbot", "napcat"), available_instance_services(_user("napcat")))
        self.assertEqual(("astrbot", "llonebot"), available_instance_services(_user("llonebot")))
        self.assertEqual((), available_instance_services(_user()))

    def test_undeployed_service_is_rejected(self):
        ensure_instance_service(_user("llonebot"), "astrbot")
        ensure_instance_service(_user("llonebot"), "llonebot")
        with self.assertRaises(HTTPException) as caught:
            ensure_instance_service(_user("llonebot"), "napcat")
        self.assertEqual(404, caught.exception.status_code)


class ServiceNavigationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.environment = Environment(loader=FileSystemLoader(PANEL_DIR / "templates"))
        cls.sidebar = cls.environment.get_template("sidebar.html")

    def render_sidebar(self, bot_type=None):
        return self.sidebar.render(
            user=_user(bot_type),
            request=SimpleNamespace(url=SimpleNamespace(path="/dashboard")),
        )

    def test_llonebot_instance_hides_napcat_logs_terminal_and_files(self):
        rendered = self.render_sidebar("llonebot")
        for prefix in ("logs", "terminal", "files"):
            self.assertIn(f'href="/{prefix}/astrbot"', rendered)
            self.assertIn(f'href="/{prefix}/llonebot"', rendered)
            self.assertNotIn(f'href="/{prefix}/napcat"', rendered)

    def test_napcat_instance_hides_llonebot_logs_terminal_and_files(self):
        rendered = self.render_sidebar("napcat")
        for prefix in ("logs", "terminal", "files"):
            self.assertIn(f'href="/{prefix}/astrbot"', rendered)
            self.assertIn(f'href="/{prefix}/napcat"', rendered)
            self.assertNotIn(f'href="/{prefix}/llonebot"', rendered)

    def test_account_without_instance_has_no_service_entries(self):
        rendered = self.render_sidebar()
        for prefix in ("logs", "terminal", "files"):
            self.assertNotIn(f'href="/{prefix}/', rendered)

    def test_file_manager_has_no_cross_service_jump(self):
        source = (PANEL_DIR / "templates" / "files.html").read_text(encoding="utf-8")
        self.assertNotIn("files.cross_service", source)

    def test_terminal_status_is_owned_by_connection_state(self):
        source = (PANEL_DIR / "templates" / "terminal.html").read_text(encoding="utf-8")
        self.assertNotIn('id="connStatus" class="conn-badge bg-warning" data-i18n=', source)
        self.assertIn("setConnectionStatus('connected')", source)


if __name__ == "__main__":
    unittest.main()
