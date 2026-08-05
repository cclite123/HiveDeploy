from __future__ import annotations

import json
import logging
import re
import threading
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from .database import SessionLocal
from .models import ImageCleanupRun, ImageSource, SiteConfig


logger = logging.getLogger(__name__)
CST = ZoneInfo("Asia/Shanghai")

DEFAULT_IMAGE_SOURCES = [
    ("Docker Hub 官方源", ""),
    ("1ms", "docker.1ms.run"),
    ("DaoCloud", "docker.m.daocloud.io"),
    ("KubeSRE", "docker.kubesre.xyz"),
    ("阿里云", "mirror.aliyuncs.com"),
    ("中科大", "docker.mirrors.ustc.edu.cn"),
    ("网易", "hub-mirror.c.163.com"),
    ("Docker 中国", "registry.docker-cn.com"),
]

MANAGED_REPOSITORIES = {
    "astrbot": "soulter/astrbot",
    "napcat": "mlikiowa/napcat-docker",
    "llonebot": "initialencounter/llonebot",
}

_cleanup_lock = threading.Lock()
_scheduler_started = False
_scheduler_lock = threading.Lock()


def normalize_registry(value: str) -> str:
    """校验并规范化为 host[:port][/path]，禁止协议、凭据和 URL 参数。"""
    value = (value or "").strip().strip("/")
    if not value:
        raise ValueError("镜像源地址不能为空")
    if "://" in value:
        raise ValueError("请勿填写 http:// 或 https:// 协议")
    if any(ch in value for ch in ("@", "?", "#", "\\")) or re.search(r"\s", value):
        raise ValueError("镜像源地址不能包含凭据、查询参数或空白字符")

    parsed = urlsplit(f"//{value}")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("镜像源地址不能包含凭据或查询参数")
    host = (parsed.hostname or "").lower()
    if not host:
        raise ValueError("镜像源域名无效")
    if re.fullmatch(r"\d{1,3}(?:\.\d{1,3}){3}", host):
        if any(int(part) > 255 for part in host.split(".")):
            raise ValueError("镜像源 IP 地址无效")
    elif any(not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
             for label in host.split(".")):
        raise ValueError("镜像源域名无效")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("镜像源端口无效") from exc
    if port is not None and not 1 <= port <= 65535:
        raise ValueError("镜像源端口无效")

    path = parsed.path.strip("/")
    if path and any(not re.fullmatch(r"[A-Za-z0-9._-]+", part) for part in path.split("/")):
        raise ValueError("镜像源路径前缀无效")
    authority = host + (f":{port}" if port is not None else "")
    return authority + (f"/{path}" if path else "")


def source_to_dict(source: ImageSource) -> dict:
    return {
        "id": source.id,
        "name": source.name,
        "registry": source.registry,
        "enabled": bool(source.enabled),
        "priority": source.priority,
        "is_default": bool(source.is_default),
        "is_official": bool(source.is_official),
    }


def seed_default_image_sources() -> None:
    db = SessionLocal()
    try:
        if db.query(ImageSource).count():
            return
        for priority, (name, registry) in enumerate(DEFAULT_IMAGE_SOURCES):
            db.add(ImageSource(
                name=name,
                registry=registry,
                enabled=True,
                priority=priority,
                is_default=priority == 0,
                is_official=priority == 0,
            ))
        db.commit()
    finally:
        db.close()


def order_image_sources(sources: List[Any], image_source_id: Optional[int] = None) -> List[Any]:
    enabled = [item for item in sources if item.enabled]
    ordered = sorted(enabled, key=lambda item: (not item.is_default, item.priority, item.id))
    if image_source_id is not None:
        selected = next((item for item in sources if item.id == image_source_id), None)
        if not selected or not selected.enabled:
            raise ValueError("所选镜像源不存在或已停用")
        ordered = [selected] + [item for item in ordered if item.id != selected.id]
    return ordered


def resolve_image_registries(image_source_id: Optional[int] = None) -> List[Optional[str]]:
    """返回拉取尝试顺序；指定源失败后仍按全站顺序回退。"""
    db = SessionLocal()
    try:
        sources = db.query(ImageSource).order_by(
            ImageSource.priority.asc(), ImageSource.id.asc()
        ).all()
        ordered = order_image_sources(sources, image_source_id)
        if not ordered:
            return [None]
        registries: List[Optional[str]] = []
        for item in ordered:
            registry = item.registry or None
            if registry not in registries:
                registries.append(registry)
        return registries or [None]
    finally:
        db.close()


def _image_service(tags: Iterable[str]) -> Optional[str]:
    for tag in tags or []:
        repository = tag.rsplit("@", 1)[0].rsplit(":", 1)[0]
        for service, managed in MANAGED_REPOSITORIES.items():
            if repository == managed or repository.endswith(f"/{managed}"):
                return service
    return None


def _referenced_image_ids(client) -> set[str]:
    referenced: set[str] = set()
    for container in client.containers.list(all=True):
        image = getattr(container, "image", None)
        image_id = getattr(image, "id", None)
        if image_id:
            referenced.add(image_id)
        attrs_id = (getattr(container, "attrs", {}) or {}).get("Image")
        if attrs_id:
            referenced.add(attrs_id)
    return referenced


def preview_cleanup(client) -> dict:
    """只分析三类受管镜像；每类最新镜像和被任何容器引用的镜像均保留。"""
    managed: Dict[str, Dict[str, Any]] = {name: {} for name in MANAGED_REPOSITORIES}
    for image in client.images.list():
        service = _image_service(getattr(image, "tags", []) or [])
        if service and image.id not in managed[service]:
            managed[service][image.id] = image

    referenced = _referenced_image_ids(client)
    entries: List[dict] = []
    for service, images_by_id in managed.items():
        images = list(images_by_id.values())
        images.sort(key=lambda image: str((getattr(image, "attrs", {}) or {}).get("Created", "")), reverse=True)
        for index, image in enumerate(images):
            image_id = image.id
            is_latest = index == 0
            is_referenced = image_id in referenced
            if is_latest:
                reason = "latest"
            elif is_referenced:
                reason = "old_but_in_use"
            else:
                reason = "old_unused"
            entries.append({
                "service": service,
                "image_id": image_id,
                "tags": list(getattr(image, "tags", []) or []),
                "created": str((getattr(image, "attrs", {}) or {}).get("Created", "")),
                "size": int((getattr(image, "attrs", {}) or {}).get("Size", 0) or 0),
                "reason": reason,
                "delete_candidate": reason == "old_unused",
            })
    return {
        "entries": entries,
        "candidate_count": sum(1 for item in entries if item["delete_candidate"]),
        "candidate_bytes": sum(item["size"] for item in entries if item["delete_candidate"]),
    }


def execute_cleanup(client) -> dict:
    """非强制删除无引用旧镜像；每次删除前重新读取全部容器引用。"""
    if not _cleanup_lock.acquire(blocking=False):
        raise RuntimeError("镜像清理任务正在执行")
    try:
        preview = preview_cleanup(client)
        deleted: List[dict] = []
        skipped: List[dict] = []
        errors: List[dict] = []
        for item in [entry for entry in preview["entries"] if entry["delete_candidate"]]:
            if item["image_id"] in _referenced_image_ids(client):
                skipped.append({**item, "reason": "new_reference_detected"})
                continue
            try:
                client.images.remove(item["image_id"], force=False, noprune=True)
                deleted.append(item)
            except Exception as exc:
                errors.append({**item, "error": str(exc)})
        return {
            "preview": preview,
            "deleted": deleted,
            "skipped": skipped,
            "errors": errors,
            "released_bytes": sum(item["size"] for item in deleted),
        }
    finally:
        _cleanup_lock.release()


def run_cleanup(client=None, *, dry_run: bool = False) -> dict:
    from .docker_manager import get_client

    client = client or get_client()
    db = SessionLocal()
    record = ImageCleanupRun(status="running", dry_run=dry_run, summary_json="{}")
    db.add(record)
    db.commit()
    db.refresh(record)
    try:
        result = preview_cleanup(client) if dry_run else execute_cleanup(client)
        record.status = "success" if dry_run or not result.get("errors") else "failed"
        record.summary_json = json.dumps(result, ensure_ascii=False)
        record.finished_at = datetime.now()
        db.commit()
        return {"run_id": record.id, "status": record.status, **result}
    except Exception as exc:
        record.status = "failed"
        record.error = str(exc)
        record.finished_at = datetime.now()
        db.commit()
        raise
    finally:
        db.close()


def _site_value(db, key: str, default: str) -> str:
    row = db.query(SiteConfig).filter_by(key=key).first()
    return row.value if row else default


def start_image_cleanup_scheduler() -> None:
    global _scheduler_started
    with _scheduler_lock:
        if _scheduler_started:
            return
        _scheduler_started = True

    def _loop():
        last_attempt_date = ""
        while True:
            try:
                db = SessionLocal()
                enabled = _site_value(db, "image_cleanup_enabled", "false") == "true"
                schedule = _site_value(db, "image_cleanup_time", "03:30")
                db.close()
                now = datetime.now(CST)
                today = now.strftime("%Y-%m-%d")
                if enabled and now.strftime("%H:%M") == schedule and last_attempt_date != today:
                    last_attempt_date = today
                    try:
                        run_cleanup()
                    except Exception:
                        logger.exception("定时镜像清理失败")
            except Exception:
                logger.exception("检查镜像清理计划失败")
            threading.Event().wait(30)

    threading.Thread(target=_loop, name="image-cleanup-scheduler", daemon=True).start()
