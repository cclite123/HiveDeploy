from __future__ import annotations

import io
import json
import os
import re
import shutil
import tarfile
import tempfile
import threading
import time
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from pathlib import PurePosixPath
from typing import Callable, Dict, Optional

import docker


DATA_DIR = os.environ.get("DATA_DIR", "/data/instances")
ASTRBOT_DATA_DIR = "/AstrBot/data"
PLUGIN_HELPER_IMAGE = os.environ.get("PLUGIN_HELPER_IMAGE", "python:3.11-slim")
MAX_ARCHIVE_BYTES = 100 * 1024 * 1024
MAX_EXTRACTED_BYTES = 200 * 1024 * 1024
MAX_ARCHIVE_FILES = 5000


class PluginInstallError(RuntimeError):
    pass


@dataclass(frozen=True)
class PluginSpec:
    key: str
    display_name: str
    directory: str
    repository: str
    archive_url: str
    ref: str


PLUGIN_CATALOG: Dict[str, PluginSpec] = {
    "xiao-xiuxian": PluginSpec(
        key="xiao-xiuxian",
        display_name="小小修仙",
        directory="astrbot_plugin_xiao_xiuxian_auto",
        repository="https://github.com/cclite123/astrbot_plugin_xiao_xiuxian_auto",
        archive_url=(
            "https://github.com/cclite123/astrbot_plugin_xiao_xiuxian_auto/"
            "archive/refs/heads/master.tar.gz"
        ),
        ref="master",
    ),
}


ProgressCallback = Callable[[str, str], None]
ArchiveFetcher = Callable[[str], bytes]


def _redact_sensitive_text(value: str) -> str:
    """Keep command output useful without exposing credentials embedded in URLs."""
    return re.sub(
        r"(?i)\b(https?|socks5h?|ftp)://([^\s/:@]+):([^\s/@]+)@",
        r"\1://\2:***@",
        str(value or ""),
    )


def get_plugin_spec(plugin_key: str) -> PluginSpec:
    spec = PLUGIN_CATALOG.get(str(plugin_key or "").strip().lower())
    if not spec:
        raise PluginInstallError("该插件不在管理员允许的一键安装白名单中")
    return spec


def _safe_username(username: str) -> str:
    username = str(username or "").strip()
    if not username or username in (".", "..") or os.path.basename(username) != username:
        raise PluginInstallError("用户名无效")
    return username


def _container_path_exists(container, path: str) -> bool:
    script = "import os,sys;raise SystemExit(0 if os.path.isdir(sys.argv[1]) else 1)"
    try:
        return container.exec_run(["python", "-c", script, path]).exit_code == 0
    except Exception:
        return False


def is_astrbot_plugin_installed(username: str, plugin_key: str, *, docker_client=None,
                                instance_key: str = "default") -> bool:
    spec = get_plugin_spec(plugin_key)
    username = _safe_username(username)
    client = docker_client or docker.from_env()
    suffix = "" if not instance_key or instance_key == "default" else f"_{instance_key}"
    container = client.containers.get(f"astrbot_{username}{suffix}")
    return _container_path_exists(container, f"{ASTRBOT_DATA_DIR}/plugins/{spec.directory}")


def _download_archive(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "HiveDeploy/1.0"})
    with urllib.request.urlopen(request, timeout=30) as response:
        declared = int(response.headers.get("Content-Length") or 0)
        if declared > MAX_ARCHIVE_BYTES:
            raise PluginInstallError("插件源码包超过 100MB 限制")
        chunks = []
        total = 0
        while True:
            chunk = response.read(1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_ARCHIVE_BYTES:
                raise PluginInstallError("插件源码包超过 100MB 限制")
            chunks.append(chunk)
    if not chunks:
        raise PluginInstallError("下载到的插件源码包为空")
    return b"".join(chunks)


def _extract_github_archive(archive: bytes, destination: str) -> None:
    try:
        package = tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz")
    except (tarfile.TarError, OSError) as exc:
        raise PluginInstallError(f"插件源码包格式无效：{exc}") from exc

    with package:
        members = package.getmembers()
        if len(members) > MAX_ARCHIVE_FILES:
            raise PluginInstallError("插件源码包文件数量超过限制")
        if sum(member.size for member in members if member.isfile()) > MAX_EXTRACTED_BYTES:
            raise PluginInstallError("插件源码包解压后超过 200MB 限制")
        roots = {
            PurePosixPath(member.name).parts[0]
            for member in members
            if PurePosixPath(member.name).parts
        }
        if len(roots) != 1:
            raise PluginInstallError("插件源码包必须只有一个顶层目录")
        root = next(iter(roots))
        for member in members:
            if "\\" in member.name:
                raise PluginInstallError("插件源码包包含不安全路径")
            path = PurePosixPath(member.name)
            if path.is_absolute() or not path.parts or path.parts[0] != root:
                raise PluginInstallError("插件源码包目录结构无效")
            relative = PurePosixPath(*path.parts[1:])
            if not relative.parts:
                continue
            if relative.is_absolute() or ".." in relative.parts:
                raise PluginInstallError("插件源码包包含不安全路径")
            if member.issym() or member.islnk() or member.isdev():
                raise PluginInstallError("插件源码包包含不允许的链接或设备文件")
            target = os.path.abspath(os.path.join(destination, *relative.parts))
            if os.path.commonpath((os.path.abspath(destination), target)) != os.path.abspath(destination):
                raise PluginInstallError("插件源码包尝试写出目标目录")
            if member.isdir():
                os.makedirs(target, exist_ok=True)
                continue
            if not member.isfile():
                continue
            source = package.extractfile(member)
            if source is None:
                raise PluginInstallError(f"无法读取插件文件：{relative}")
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with source, open(target, "wb") as output:
                shutil.copyfileobj(source, output)


def _pack_directory(source: str) -> bytes:
    archive = io.BytesIO()
    with tarfile.open(fileobj=archive, mode="w") as package:
        package.add(source, arcname="source", recursive=True)
    return archive.getvalue()


_VOLUME_MUTATION_SCRIPT = r"""
import os
import shutil
import sys

mode = sys.argv[1]
source, target, backup, failed = sys.argv[2:6]
installed_before = sys.argv[6] == "1"

if mode == "switch":
    os.makedirs(os.path.dirname(target), exist_ok=True)
    os.makedirs(os.path.dirname(backup), exist_ok=True)
    if installed_before:
        old_data = os.path.join(target, "data")
        if os.path.isdir(old_data):
            shutil.copytree(old_data, os.path.join(source, "data"), dirs_exist_ok=True)
        for filename in ("config.json", ".env"):
            old_file = os.path.join(target, filename)
            if os.path.isfile(old_file):
                shutil.copy2(old_file, os.path.join(source, filename))
        shutil.move(target, backup)
    os.replace(source, target)
elif mode == "rollback":
    if installed_before:
        if os.path.isdir(backup):
            if os.path.isdir(target):
                shutil.move(target, failed)
            shutil.move(backup, target)
    elif os.path.isdir(target):
        shutil.move(target, failed)
else:
    raise RuntimeError("unknown mutation mode")
"""


def _run_volume_mutation(client, container, mode: str, source: str, target: str,
                         backup: str, failed: str, installed_before: bool) -> None:
    try:
        client.containers.run(
            PLUGIN_HELPER_IMAGE,
            [
                "python", "-c", _VOLUME_MUTATION_SCRIPT, mode,
                source, target, backup, failed, "1" if installed_before else "0",
            ],
            volumes_from=[container.id],
            network_disabled=True,
            remove=True,
        )
    except Exception as exc:
        detail = getattr(exc, "stderr", None) or str(exc)
        if isinstance(detail, bytes):
            detail = detail.decode("utf-8", errors="replace")
        raise PluginInstallError(
            f"插件文件{('切换' if mode == 'switch' else '回滚')}失败："
            f"{_redact_sensitive_text(str(detail))[-1200:]}"
        ) from exc


class AstrBotPluginInstaller:
    """Install one allow-listed plugin behind a single, rollback-safe interface."""

    def __init__(
        self,
        *,
        data_dir: str = DATA_DIR,
        docker_client=None,
        archive_fetcher: ArchiveFetcher = _download_archive,
        instance_key: str = "default",
    ):
        self.data_dir = data_dir
        self.docker_client = docker_client
        self.archive_fetcher = archive_fetcher
        self.instance_key = instance_key or "default"

    def install(
        self,
        username: str,
        spec: PluginSpec,
        progress: Optional[ProgressCallback] = None,
    ) -> dict:
        report = progress or (lambda _step, _detail="": None)
        username = _safe_username(username)

        client = self.docker_client or docker.from_env()
        try:
            suffix = "" if self.instance_key == "default" else f"_{self.instance_key}"
            container = client.containers.get(f"astrbot_{username}{suffix}")
            container.reload()
        except Exception as exc:
            raise PluginInstallError(f"无法读取 AstrBot 容器：{exc}") from exc
        if getattr(container, "status", "") != "running":
            raise PluginInstallError("AstrBot 容器需要处于 running 状态")

        stage = tempfile.mkdtemp(prefix=f"{spec.directory}-")
        source = os.path.join(stage, "source")
        os.makedirs(source)
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        plugins_root = f"{ASTRBOT_DATA_DIR}/plugins"
        backups_root = f"{ASTRBOT_DATA_DIR}/plugin_backups"
        staging_root = f"{ASTRBOT_DATA_DIR}/.plugin-staging"
        remote_stage = f"{staging_root}/{spec.directory}-{timestamp}"
        remote_source = f"{remote_stage}/source"
        target = f"{plugins_root}/{spec.directory}"
        backup = f"{backups_root}/{spec.directory}.preinstall-{timestamp}"
        failed = f"{backups_root}/{spec.directory}.failed-{timestamp}"
        installed_before = _container_path_exists(container, target)
        mutation_started = False

        try:
            report("正在下载插件源码...", spec.repository)
            archive = self.archive_fetcher(spec.archive_url)
            report("正在校验插件源码...", f"{len(archive) / 1024 / 1024:.1f}MB")
            _extract_github_archive(archive, source)
            for required in ("main.py", "metadata.yaml"):
                if not os.path.isfile(os.path.join(source, required)):
                    raise PluginInstallError(f"插件源码缺少必要文件：{required}")

            with open(os.path.join(source, ".installed_source.json"), "w", encoding="utf-8") as marker:
                json.dump({
                    "repository": spec.repository,
                    "ref": spec.ref,
                    "installed_at": datetime.now().isoformat(timespec="seconds"),
                }, marker, ensure_ascii=False, indent=2)

            mkdir_script = "import os,sys;os.makedirs(sys.argv[1],exist_ok=True)"
            result = container.exec_run(["python", "-c", mkdir_script, remote_stage])
            if result.exit_code != 0:
                detail = result.output.decode("utf-8", errors="replace")[-1200:]
                raise PluginInstallError(f"无法创建插件暂存目录：{_redact_sensitive_text(detail)}")
            if not container.put_archive(remote_stage, _pack_directory(source)):
                raise PluginInstallError("无法把插件源码写入 AstrBot 数据卷")

            requirements = os.path.join(source, "requirements.txt")
            if os.path.isfile(requirements) and os.path.getsize(requirements) > 0:
                report("正在安装插件依赖...", "首次安装可能需要几分钟")
                result = container.exec_run([
                    "python", "-m", "pip", "install", "--disable-pip-version-check",
                    "-r", f"{remote_source}/requirements.txt",
                ])
                if result.exit_code != 0:
                    detail = _redact_sensitive_text(
                        result.output.decode("utf-8", errors="replace")[-1200:]
                    )
                    raise PluginInstallError(f"插件依赖安装失败：{detail}")

            if installed_before:
                report("正在保留现有配置和数据...", "升级不会清空插件运行数据")
            report("正在备份并切换插件...", "AstrBot 将短暂重启")
            container.stop(timeout=30)
            mutation_started = True
            _run_volume_mutation(
                client, container, "switch", remote_source, target,
                backup, failed, installed_before,
            )
            started_at = int(time.time())
            container.start()
            time.sleep(8)
            container.reload()
            if getattr(container, "status", "") != "running":
                raise PluginInstallError("AstrBot 重启后未进入 running 状态")

            try:
                logs = container.logs(since=started_at).decode("utf-8", errors="replace")
            except Exception:
                logs = ""
            import_failure = re.compile(
                rf"(?:Failed to import plugin\s+{re.escape(spec.directory)}|"
                rf"Plugin\s+{re.escape(spec.directory)}\s+failed to import|"
                rf"No module named ['\"]data\.plugins\.{re.escape(spec.directory)})",
                re.IGNORECASE,
            )
            failed_lines = [line for line in logs.splitlines() if import_failure.search(line)]
            if failed_lines:
                raise PluginInstallError(f"AstrBot 加载插件失败：{failed_lines[-1][-500:]}")

            action = "updated" if installed_before else "installed"
            return {
                "ok": True,
                "action": action,
                "plugin": spec.display_name,
                "directory": spec.directory,
                "repository": spec.repository,
                "backup": backup if installed_before else "",
            }
        except Exception as exc:
            rollback_error = None
            if mutation_started:
                try:
                    container.stop(timeout=10)
                except Exception:
                    pass
                try:
                    _run_volume_mutation(
                        client, container, "rollback", remote_source, target,
                        backup, failed, installed_before,
                    )
                except Exception as rollback_exc:
                    rollback_error = rollback_exc
                finally:
                    try:
                        container.start()
                    except Exception:
                        pass
            if rollback_error:
                raise PluginInstallError(
                    f"{_redact_sensitive_text(str(exc))}；自动回滚失败："
                    f"{_redact_sensitive_text(str(rollback_error))}"
                ) from exc
            if isinstance(exc, PluginInstallError):
                raise
            raise PluginInstallError(_redact_sensitive_text(str(exc))) from exc
        finally:
            try:
                container.reload()
                if getattr(container, "status", "") == "running":
                    cleanup_script = "import shutil,sys;shutil.rmtree(sys.argv[1],ignore_errors=True)"
                    container.exec_run(["python", "-c", cleanup_script, remote_stage])
            except Exception:
                pass
            shutil.rmtree(stage, ignore_errors=True)


def install_astrbot_plugin(username: str, plugin_key: str, progress: Optional[ProgressCallback] = None,
                           instance_key: str = "default") -> dict:
    return AstrBotPluginInstaller(instance_key=instance_key).install(username, get_plugin_spec(plugin_key), progress)


def start_astrbot_plugin_install(username: str, plugin_key: str, instance_key: str = "default") -> dict:
    from .progress_store import start_task, update_task

    spec = get_plugin_spec(plugin_key)
    task = start_task(username, "plugin_install", f"plugin:{spec.key}", "准备安装插件...", instance_key=instance_key)

    def run():
        try:
            result = install_astrbot_plugin(
                username,
                spec.key,
                lambda step, detail="": update_task(task["task_id"], step, detail),
                instance_key=instance_key,
            )
            verb = "更新" if result["action"] == "updated" else "安装"
            update_task(
                task["task_id"],
                f"{spec.display_name}{verb}完成",
                f"插件目录：{result['directory']}",
                done=True,
            )
        except Exception as exc:
            update_task(
                task["task_id"],
                f"{spec.display_name}安装失败",
                error=_redact_sensitive_text(str(exc)),
            )

    threading.Thread(target=run, daemon=True).start()
    return task
