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

    def test_napcat_connection_still_uses_one_reverse_client(self):
        config = docker_manager._configure_napcat_connection(
            {"keep": True, "network": {"websocketClients": [{"old": True}]}},
            "ws://host:6199/ws", "token",
        )
        self.assertTrue(config["keep"])
        clients = config["network"]["websocketClients"]
        self.assertEqual(1, len(clients))
        self.assertEqual("token", clients[0]["token"])

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


if __name__ == "__main__":
    unittest.main()
