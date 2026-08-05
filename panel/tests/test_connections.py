import json
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("DB_PATH", os.path.join(tempfile.gettempdir(), f"hivedeploy-tests-{os.getpid()}.db"))

from app import docker_manager


class ConnectionConfigTests(unittest.TestCase):
    def test_llonebot_815_connection_uses_shared_token_and_preserves_other_settings(self):
        token = "shared-token"
        astrbot = docker_manager._configure_astrbot_platform({"unrelated": 1}, token)
        llonebot = docker_manager._configure_llonebot_connection(
            {"account": {"uin": "123456"}, "ob11": {"connect": [{"old": True}]}},
            "ws://example.com:20002/ws", token,
        )

        self.assertEqual(1, astrbot["unrelated"])
        self.assertEqual(["aiocqhttp"], [item["type"] for item in astrbot["platform"]])
        self.assertEqual(token, astrbot["platform"][0]["ws_reverse_token"])
        self.assertEqual("123456", llonebot["account"]["uin"])
        self.assertEqual(1, len(llonebot["ob11"]["connect"]))
        connection = llonebot["ob11"]["connect"][0]
        self.assertEqual("ws-reverse", connection["type"])
        self.assertEqual("ws://example.com:20002/ws", connection["url"])
        self.assertEqual(token, connection["token"])
        self.assertEqual(60000, connection["heartInterval"])
        self.assertTrue(connection["reportSelfMessage"])
        self.assertTrue(connection["debug"])

    def test_napcat_connection_still_uses_one_reverse_client(self):
        config = docker_manager._configure_napcat_connection(
            {"keep": True, "network": {"websocketClients": [{"old": True}]}},
            "ws://host:6199/ws", "token",
        )
        self.assertTrue(config["keep"])
        clients = config["network"]["websocketClients"]
        self.assertEqual(1, len(clients))
        self.assertEqual("token", clients[0]["token"])
        self.assertTrue(clients[0]["reportSelfMessage"])
        self.assertTrue(clients[0]["debug"])

    def test_missing_or_ambiguous_account_configuration_stops_without_writing(self):
        client = SimpleNamespace(containers=SimpleNamespace(get=lambda _: object()))
        common = [
            patch.object(docker_manager, "get_instance_status", return_value={"astrbot": "running", "llonebot": "running"}),
            patch.object(docker_manager, "get_client", return_value=client),
            patch.object(docker_manager, "_find_container_file", return_value="/AstrBot/data/cmd_config.json"),
        ]
        for account_paths, expected in [([], "扫码登录"), (["config_1.json", "config_2.json"], "多个")]:
            with self.subTest(paths=account_paths), common[0], common[1], common[2], \
                    patch.object(docker_manager, "_find_container_files", return_value=account_paths), \
                    patch.object(docker_manager, "_write_container_json") as write:
                result = docker_manager.configure_bot_astrbot("alice", "llonebot", "ws://host/ws")
                self.assertFalse(result["ok"])
                self.assertIn(expected, result["error"])
                write.assert_not_called()

    def test_llonebot_account_discovery_does_not_require_find_binary(self):
        astrbot = SimpleNamespace(stop=lambda timeout=20: None, start=lambda: None)
        llonebot = SimpleNamespace(stop=lambda timeout=20: None, start=lambda: None)
        containers = SimpleNamespace(get=lambda name: astrbot if name.startswith("astrbot_") else llonebot)
        discovery_commands = []

        def discover(_container, command):
            discovery_commands.append(command)
            return ["/root/llonebot/data/config_123.json"]

        with patch.object(docker_manager, "get_instance_status", return_value={"astrbot": "running", "llonebot": "running"}), \
                patch.object(docker_manager, "get_client", return_value=SimpleNamespace(containers=containers)), \
                patch.object(docker_manager, "_find_container_file", return_value="/AstrBot/data/cmd_config.json"), \
                patch.object(docker_manager, "_find_container_files", side_effect=discover), \
                patch.object(docker_manager, "_read_container_json", side_effect=[{}, {"ob11": {}}]), \
                patch.object(docker_manager, "_write_persistent_json"):
            result = docker_manager.configure_bot_astrbot("alice", "llonebot", "ws://host/ws")

        self.assertTrue(result["ok"])
        self.assertIn("for p in /root/llonebot/data/config_*.json", discovery_commands[0])
        self.assertNotIn("find /root/llonebot/data", discovery_commands[0])

    def test_auto_config_stops_services_before_persistent_writes(self):
        events = []

        class Container:
            def __init__(self, name):
                self.name = name

            def stop(self, timeout=10):
                events.append(f"stop:{self.name}")

            def start(self):
                events.append(f"start:{self.name}")

        astrbot = Container("astrbot")
        llonebot = Container("llonebot")
        containers = SimpleNamespace(get=lambda name: astrbot if name.startswith("astrbot_") else llonebot)

        def persistent_write(_username, service, _path, _config):
            events.append(f"write:{service}")

        with patch.object(docker_manager, "get_instance_status", return_value={"astrbot": "running", "llonebot": "running"}), \
                patch.object(docker_manager, "get_client", return_value=SimpleNamespace(containers=containers)), \
                patch.object(docker_manager, "_find_container_file", return_value="/AstrBot/data/cmd_config.json"), \
                patch.object(docker_manager, "_find_container_files", return_value=["/root/llonebot/data/config_123.json"]), \
                patch.object(docker_manager, "_read_container_json", side_effect=[{}, {"ob11": {}}]), \
                patch.object(docker_manager, "_write_persistent_json", create=True, side_effect=persistent_write), \
                patch.object(docker_manager, "_write_container_json"), \
                patch.object(docker_manager, "restart_user_instance"):
            result = docker_manager.configure_bot_astrbot("alice", "llonebot", "ws://host/ws")

        self.assertTrue(result["ok"])
        self.assertEqual([
            "stop:astrbot", "stop:llonebot",
            "write:llonebot", "write:astrbot",
            "start:astrbot", "start:llonebot",
        ], events)

    def test_persistent_llonebot_config_is_written_atomically_to_data_mount(self):
        with tempfile.TemporaryDirectory() as data_dir, \
                patch.object(docker_manager, "DATA_DIR", data_dir):
            destination = docker_manager._write_persistent_json(
                "alice", "llonebot", "/root/llonebot/data/config_123.json",
                {"ob11": {"connect": []}},
            )
            self.assertEqual(
                os.path.join(data_dir, "alice", "llonebot", ".llonebot-data", "config_123.json"),
                destination,
            )
            with open(destination, encoding="utf-8") as config_file:
                self.assertEqual({"ob11": {"connect": []}}, json.load(config_file))

    def test_llonebot_mounts_real_data_directory(self):
        captured = {}

        class Containers:
            def run(self, *args, **kwargs):
                captured.update(kwargs)
                return SimpleNamespace(id="container")

        with tempfile.TemporaryDirectory() as data_dir, \
                patch.object(docker_manager, "_container_resource_kwargs", return_value={}):
            docker_manager._run_llonebot(
                SimpleNamespace(containers=Containers()), "alice", 1,
                {"napcat_web": 20001}, data_dir, {},
            )
            persistent = os.path.join(data_dir, "llonebot", ".llonebot-data")
            self.assertEqual("/root/llonebot/data", captured["volumes"][persistent]["bind"])
            self.assertTrue(os.path.isdir(persistent))

    def test_llonebot_uses_persisted_auth_and_existing_web_proxy(self):
        captured = {}

        class Containers:
            def get(self, name):
                self.last_get = name
                return SimpleNamespace(status="running")

            def run(self, *args, **kwargs):
                captured.update(kwargs)
                return SimpleNamespace(id="container")

        containers = Containers()
        with tempfile.TemporaryDirectory() as data_dir, \
                patch.object(docker_manager, "_container_resource_kwargs", return_value={}):
            persistent = os.path.join(data_dir, "llonebot", ".llonebot-data")
            os.makedirs(persistent)
            with open(os.path.join(persistent, "auth_token.txt"), "w", encoding="utf-8") as auth_file:
                auth_file.write("persisted-auth")
            with open(os.path.join(persistent, "config_123456.json"), "w", encoding="utf-8") as account_file:
                account_file.write("{}")
            docker_manager._run_llonebot(
                SimpleNamespace(containers=containers), "alice", 1,
                {"napcat_web": 20001}, data_dir, {},
            )

        self.assertEqual("llonebot_web_proxy_alice", containers.last_get)
        self.assertEqual({}, captured["ports"])
        self.assertEqual("persisted-auth", captured["environment"]["AUTH_TOKEN"])
        self.assertEqual("123456", captured["environment"]["QUICK_LOGIN_QQ"])


if __name__ == "__main__":
    unittest.main()
