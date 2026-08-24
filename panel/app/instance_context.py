"""实例级上下文：集中处理归属、命名、目录和端口规则。

旧的 ``default`` 实例故意保留旧容器名和目录；其它实例才使用内部 key
隔离目录/容器。路由和后台任务只需要传入 Instance，不再自行拼接这些值。
"""
from __future__ import annotations

import json
import os
import re
import secrets
from typing import Optional

from fastapi import HTTPException
from sqlalchemy.orm import Session

from .models import Instance, User


def normalize_instance_key(value: str) -> str:
    value = re.sub(r"[^a-zA-Z0-9_-]+", "-", (value or "").strip()).strip("-_").lower()
    return value[:48] or secrets.token_hex(6)


def instance_key(instance: Instance) -> str:
    return (getattr(instance, "instance_key", None) or "default").strip() or "default"


def is_legacy_instance(instance: Instance) -> bool:
    return instance_key(instance) == "default"


def storage_root(instance: Instance, username: Optional[str] = None) -> str:
    root = getattr(instance, "storage_root", None)
    if root:
        return root
    username = username or getattr(getattr(instance, "user", None), "username", "")
    base = os.environ.get("DATA_DIR", "/data/instances")
    if is_legacy_instance(instance):
        return os.path.join(base, username)
    return os.path.join(base, username, "instances", instance_key(instance))


def service_data_dir(instance: Instance, username: str, service: str) -> str:
    return os.path.join(storage_root(instance, username), service)


def container_name(instance_or_username, username_or_service: str, service_or_key: str) -> str:
    """生成容器名，兼容 ``(Instance, username, service)`` 和
    ``(username, service, instance_key)`` 两种调用形式。"""
    if isinstance(instance_or_username, Instance):
        instance = instance_or_username
        username, service = username_or_service, service_or_key
        key = instance_key(instance)
    else:
        username, service, key = instance_or_username, username_or_service, service_or_key
    suffix = "" if not key or key == "default" else f"_{key}"
    return f"{service}_{username}{suffix}"


def port_base(instance: Instance, user_id: Optional[int] = None) -> int:
    current = getattr(instance, "port_base", None)
    if current:
        return int(current)
    base = int(os.environ.get("INSTANCE_PORT_BASE", "20000"))
    return base + ((int(user_id or getattr(instance, "user_id", 1)) - 1) * 10)


def calc_instance_ports(instance: Instance) -> dict[str, int]:
    base = port_base(instance)
    return {"astrbot_web": base, "napcat_web": base + 1, "astrbot_ws": base + 2}


def calc_instance_extra_ports(instance: Instance) -> list[int]:
    base = port_base(instance)
    return list(range(base + 3, base + 10))


def find_available_port_base(db: Session, *, exclude_instance_id: Optional[int] = None) -> int:
    """扫描完整 10 端口区间，避开 DB 记录和当前 Docker 映射。"""
    start = int(os.environ.get("INSTANCE_PORT_BASE", "20000"))
    used: set[int] = set()
    rows = db.query(Instance).all()
    for row in rows:
        if exclude_instance_id and row.id == exclude_instance_id:
            continue
        if row.port_base:
            used.update(range(int(row.port_base), int(row.port_base) + 10))
        else:
            used.update({row.astrbot_port, row.napcat_web_port, row.astrbot_ws_port})
        try:
            used.update(int(item.get("host_port")) for item in json.loads(row.extra_ports_json or "[]")
                        if item.get("host_port") is not None)
        except Exception:
            pass
    try:
        import docker
        client = docker.from_env()
        for container in client.containers.list(all=True):
            for bindings in (container.ports or {}).values():
                for binding in bindings or []:
                    try:
                        used.add(int(binding["HostPort"]))
                    except (KeyError, TypeError, ValueError):
                        pass
    except Exception:
        # Docker 不可用时仍可依据 DB 分配，创建阶段会返回明确的端口冲突。
        pass
    candidate = start
    while candidate < start + 100000:
        if not any(port in used for port in range(candidate, candidate + 10)):
            return candidate
        candidate += 10
    raise RuntimeError("没有可用的实例端口区间")


def get_instance(db: Session, user: User, instance_id: Optional[int] = None,
                 *, allow_admin: bool = False) -> Optional[Instance]:
    query = db.query(Instance)
    if instance_id is not None:
        query = query.filter(Instance.id == int(instance_id))
        row = query.first()
        if row and (row.user_id == user.id or (allow_admin and user.is_admin)):
            return row
        if row:
            raise HTTPException(status_code=404, detail="实例不存在")
        return None
    rows = query.filter(Instance.user_id == user.id).order_by(Instance.created_at.asc(), Instance.id.asc()).all()
    return next((row for row in rows if instance_key(row) == "default"), rows[0] if rows else None)


def list_user_instances(db: Session, user: User) -> list[Instance]:
    return db.query(Instance).filter_by(user_id=user.id).order_by(
        Instance.created_at.asc(), Instance.id.asc()
    ).all()


def instance_quota(user: User, count: int) -> dict:
    enabled = bool(getattr(user, "multi_instance_enabled", False) or user.is_admin)
    limit = max(1, int(getattr(user, "max_instances", 1) or 1))
    if user.is_admin and limit < 2:
        limit = 2
    return {"enabled": enabled, "count": count, "max_instances": limit,
            "remaining": max(0, limit - count), "can_create": enabled or count < 1}


def assert_can_create(user: User, count: int) -> dict:
    quota = instance_quota(user, count)
    if count >= quota["max_instances"]:
        raise HTTPException(status_code=409, detail=f"已达到实例上限（{quota['max_instances']} 个）")
    if count >= 1 and not quota["enabled"]:
        raise HTTPException(status_code=403, detail="管理员尚未开放多实例权限")
    return quota


def instance_to_dict(instance: Instance, *, status: Optional[dict] = None, user=None) -> dict:
    try:
        extra = json.loads(instance.extra_ports_json or "[]")
    except Exception:
        extra = []
    return {
        "id": instance.id,
        "instance_key": instance_key(instance),
        "display_name": instance.display_name or ("默认实例" if is_legacy_instance(instance) else instance_key(instance)),
        "bot_type": instance.bot_type or "napcat",
        "status": instance.status or "unknown",
        "container_status": status or {},
        "astrbot_port": instance.astrbot_port,
        "napcat_web_port": instance.napcat_web_port,
        "astrbot_ws_port": instance.astrbot_ws_port,
        "port_base": port_base(instance),
        "extra_ports": extra,
        "storage_root": storage_root(instance, getattr(user, "username", None)),
        "created_at": instance.created_at.isoformat() if instance.created_at else None,
    }
