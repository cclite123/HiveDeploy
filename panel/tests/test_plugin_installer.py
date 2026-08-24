import io
import os
import tarfile
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from app.plugin_installer import (
    AstrBotPluginInstaller,
    PluginInstallError,
    get_plugin_spec,
    is_astrbot_plugin_installed,
    start_astrbot_plugin_install,
)


def plugin_archive(files=None):
    files = files or {
        "main.py": b"PLUGIN = True\n",
        "metadata.yaml": b"name: test\n",
        "requirements.txt": b"example-package>=1\n",
        "data/default.txt": b"new-default\n",
    }
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz") as package:
        for name, content in files.items():
            info = tarfile.TarInfo(f"repo-master/{name}")
            info.size = len(content)
            package.addfile(info, io.BytesIO(content))
    return output.getvalue()


class FakeContainer:
    def __init__(self, *, dependency_exit=0, start_error=None, log_output=None):
        self.id = "fake-astrbot-container"
        self.status = "running"
        self.dependency_exit = dependency_exit
        self.dependency_output = b"dependency output"
        self.start_error = start_error
        self.log_output = log_output or b"Plugin astrbot_plugin_xiao_xiuxian_auto loaded"
        self.events = []
        self.data_root = None

    def local_path(self, container_path):
        prefix = "/AstrBot/data"
        assert container_path == prefix or container_path.startswith(prefix + "/")
        relative = container_path[len(prefix):].lstrip("/")
        return os.path.join(self.data_root, *relative.split("/")) if relative else self.data_root

    def reload(self):
        self.events.append("reload")

    def exec_run(self, command):
        self.events.append(("exec", command))
        if command[:4] == ["python", "-m", "pip", "install"]:
            requirements = self.local_path(command[command.index("-r") + 1])
            if not os.path.isfile(requirements):
                return SimpleNamespace(exit_code=1, output=f"No such file: {requirements}".encode())
            return SimpleNamespace(exit_code=self.dependency_exit, output=self.dependency_output)
        if command[:2] == ["python", "-c"] and len(command) >= 4:
            script, path = command[2], command[3]
            local = self.local_path(path)
            if "os.path.isdir" in script:
                return SimpleNamespace(exit_code=0 if os.path.isdir(local) else 1, output=b"")
            if "os.makedirs" in script:
                os.makedirs(local, exist_ok=True)
            elif "shutil.rmtree" in script:
                import shutil
                shutil.rmtree(local, ignore_errors=True)
        return SimpleNamespace(exit_code=0, output=b"")

    def stop(self, timeout=10):
        self.events.append("stop")
        self.status = "exited"

    def start(self):
        self.events.append("start")
        if self.start_error:
            raise self.start_error
        self.status = "running"

    def logs(self, since=None):
        return self.log_output

    def put_archive(self, path, data):
        self.events.append(("put_archive", path, len(data)))
        target = self.local_path(path)
        os.makedirs(target, exist_ok=True)
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:") as package:
            package.extractall(target, filter="data")
        return True


class FakeContainers:
    def __init__(self, container):
        self.container = container

    def get(self, _name):
        return self.container

    def run(self, _image, command, **kwargs):
        self.container.events.append(("helper", command, kwargs))
        mode, source, target, backup, failed, installed = command[3:9]
        source = self.container.local_path(source)
        target = self.container.local_path(target)
        backup = self.container.local_path(backup)
        failed = self.container.local_path(failed)
        installed = installed == "1"
        import shutil
        if mode == "switch":
            os.makedirs(os.path.dirname(target), exist_ok=True)
            os.makedirs(os.path.dirname(backup), exist_ok=True)
            if installed:
                old_data = os.path.join(target, "data")
                if os.path.isdir(old_data):
                    shutil.copytree(old_data, os.path.join(source, "data"), dirs_exist_ok=True)
                for filename in ("config.json", ".env"):
                    old_file = os.path.join(target, filename)
                    if os.path.isfile(old_file):
                        shutil.copy2(old_file, os.path.join(source, filename))
                shutil.move(target, backup)
            os.replace(source, target)
        elif installed:
            if os.path.isdir(backup):
                if os.path.isdir(target):
                    shutil.move(target, failed)
                shutil.move(backup, target)
        elif os.path.isdir(target):
            shutil.move(target, failed)
        return b""


class PluginInstallerTests(unittest.TestCase):
    def installer(self, data_dir, container, archive=None):
        container.data_root = os.path.join(data_dir, "alice", "astrbot")
        os.makedirs(container.data_root, exist_ok=True)
        client = SimpleNamespace(containers=FakeContainers(container))
        return AstrBotPluginInstaller(
            data_dir=data_dir,
            docker_client=client,
            archive_fetcher=lambda _url: archive or plugin_archive(),
        )

    def test_unknown_plugin_is_rejected_by_allowlist(self):
        with self.assertRaises(PluginInstallError):
            get_plugin_spec("arbitrary-repository")

    def test_fresh_install_validates_installs_dependencies_and_restarts(self):
        spec = get_plugin_spec("xiao-xiuxian")
        container = FakeContainer()
        with tempfile.TemporaryDirectory() as data_dir, patch("app.plugin_installer.time.sleep"):
            installer = self.installer(data_dir, container)
            result = installer.install("alice", spec)
            target = os.path.join(data_dir, "alice", "astrbot", "plugins", spec.directory)

            self.assertEqual("installed", result["action"])
            self.assertTrue(os.path.isfile(os.path.join(target, "main.py")))
            self.assertTrue(os.path.isfile(os.path.join(target, ".installed_source.json")))
            self.assertTrue(is_astrbot_plugin_installed(
                "alice", spec.key, docker_client=installer.docker_client,
            ))
            self.assertIn("stop", container.events)
            self.assertIn("start", container.events)
            command = next(
                event[1] for event in container.events
                if isinstance(event, tuple) and event[0] == "exec"
                and event[1][:4] == ["python", "-m", "pip", "install"]
            )
            self.assertEqual(["python", "-m", "pip", "install"], command[:4])
            self.assertTrue(any(event[0] == "put_archive" for event in container.events if isinstance(event, tuple)))

    def test_update_preserves_runtime_data_and_root_config(self):
        spec = get_plugin_spec("xiao-xiuxian")
        container = FakeContainer()
        with tempfile.TemporaryDirectory() as data_dir, patch("app.plugin_installer.time.sleep"):
            old = os.path.join(data_dir, "alice", "astrbot", "plugins", spec.directory)
            os.makedirs(os.path.join(old, "data"), exist_ok=True)
            with open(os.path.join(old, "data", "state.json"), "w", encoding="utf-8") as handle:
                handle.write("runtime-state")
            with open(os.path.join(old, "config.json"), "w", encoding="utf-8") as handle:
                handle.write('{"custom": true}')

            result = self.installer(data_dir, container).install("alice", spec)

            self.assertEqual("updated", result["action"])
            self.assertTrue(os.path.isdir(container.local_path(result["backup"])))
            with open(os.path.join(old, "data", "state.json"), encoding="utf-8") as handle:
                self.assertEqual("runtime-state", handle.read())
            with open(os.path.join(old, "config.json"), encoding="utf-8") as handle:
                self.assertEqual('{"custom": true}', handle.read())

    def test_dependency_failure_leaves_existing_plugin_untouched(self):
        spec = get_plugin_spec("xiao-xiuxian")
        container = FakeContainer(dependency_exit=1)
        with tempfile.TemporaryDirectory() as data_dir, patch("app.plugin_installer.time.sleep"):
            old = os.path.join(data_dir, "alice", "astrbot", "plugins", spec.directory)
            os.makedirs(old, exist_ok=True)
            marker = os.path.join(old, "old.py")
            with open(marker, "w", encoding="utf-8") as handle:
                handle.write("old")

            with self.assertRaisesRegex(PluginInstallError, "依赖安装失败"):
                self.installer(data_dir, container).install("alice", spec)

            self.assertTrue(os.path.isfile(marker))
            self.assertNotIn("stop", container.events)

    def test_restart_failure_rolls_back_previous_plugin(self):
        spec = get_plugin_spec("xiao-xiuxian")
        container = FakeContainer(start_error=RuntimeError("start failed"))
        with tempfile.TemporaryDirectory() as data_dir, patch("app.plugin_installer.time.sleep"):
            old = os.path.join(data_dir, "alice", "astrbot", "plugins", spec.directory)
            os.makedirs(old, exist_ok=True)
            marker = os.path.join(old, "old.py")
            with open(marker, "w", encoding="utf-8") as handle:
                handle.write("old")

            with self.assertRaisesRegex(PluginInstallError, "start failed"):
                self.installer(data_dir, container).install("alice", spec)

            self.assertTrue(os.path.isfile(marker))

    def test_import_failure_log_rolls_back_but_unrelated_plugin_error_does_not(self):
        spec = get_plugin_spec("xiao-xiuxian")
        with tempfile.TemporaryDirectory() as data_dir, patch("app.plugin_installer.time.sleep"):
            old = os.path.join(data_dir, "alice", "astrbot", "plugins", spec.directory)
            os.makedirs(old, exist_ok=True)
            marker = os.path.join(old, "old.py")
            with open(marker, "w", encoding="utf-8") as handle:
                handle.write("old")
            failed = FakeContainer(log_output=b"Failed to import plugin astrbot_plugin_xiao_xiuxian_auto")
            with self.assertRaisesRegex(PluginInstallError, "加载插件失败"):
                self.installer(data_dir, failed).install("alice", spec)
            self.assertTrue(os.path.isfile(marker))

        with tempfile.TemporaryDirectory() as data_dir, patch("app.plugin_installer.time.sleep"):
            harmless = FakeContainer(log_output=b"[ERROR] astrbot_plugin_xiao_xiuxian_auto remote price unavailable")
            result = self.installer(data_dir, harmless).install("alice", spec)
            self.assertEqual("installed", result["action"])

    def test_archive_path_traversal_is_rejected(self):
        spec = get_plugin_spec("xiao-xiuxian")
        bad_archive = plugin_archive({
            "main.py": b"pass\n",
            "metadata.yaml": b"name: test\n",
            "../escape.py": b"bad\n",
        })
        with tempfile.TemporaryDirectory() as data_dir:
            with self.assertRaisesRegex(PluginInstallError, "不安全路径"):
                self.installer(data_dir, FakeContainer(), bad_archive).install("alice", spec)

    def test_archive_backslash_path_traversal_is_rejected(self):
        spec = get_plugin_spec("xiao-xiuxian")
        bad_archive = plugin_archive({
            "main.py": b"pass\n",
            "metadata.yaml": b"name: test\n",
            "..\\escape.py": b"bad\n",
        })
        with tempfile.TemporaryDirectory() as data_dir:
            with self.assertRaisesRegex(PluginInstallError, "不安全路径"):
                self.installer(data_dir, FakeContainer(), bad_archive).install("alice", spec)

    def test_dependency_error_redacts_credentials_from_url(self):
        spec = get_plugin_spec("xiao-xiuxian")
        container = FakeContainer(dependency_exit=1)
        container.dependency_output = (
            b"Could not fetch https://proxy-user:proxy-password@example.test/simple"
        )
        with tempfile.TemporaryDirectory() as data_dir, patch("app.plugin_installer.time.sleep"):
            with self.assertRaises(PluginInstallError) as raised:
                self.installer(data_dir, container).install("alice", spec)

        message = str(raised.exception)
        self.assertIn("https://proxy-user:***@example.test/simple", message)
        self.assertNotIn("proxy-password", message)

    def test_background_task_persists_progress_and_success(self):
        updates = []

        class ImmediateThread:
            def __init__(self, target, daemon):
                self.target = target

            def start(self):
                self.target()

        with patch("app.progress_store.start_task", return_value={"task_id": "task-1"}), \
                patch("app.progress_store.update_task", side_effect=lambda *args, **kwargs: updates.append((args, kwargs))), \
                patch("app.plugin_installer.install_astrbot_plugin", return_value={
                    "action": "installed", "directory": "astrbot_plugin_xiao_xiuxian_auto",
                }), \
                patch("app.plugin_installer.threading.Thread", ImmediateThread):
            task = start_astrbot_plugin_install("alice", "xiao-xiuxian")

        self.assertEqual("task-1", task["task_id"])
        self.assertTrue(updates[-1][1]["done"])
        self.assertIn("安装完成", updates[-1][0][1])


if __name__ == "__main__":
    unittest.main()
