import json
import re
import logging
import secrets
from datetime import datetime
from typing import Optional

import docker as docker_lib
from fastapi import APIRouter, Request, Depends, HTTPException, Body, Query
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from sqlalchemy.orm import Session

from .bootstrap import templates
from .database import get_db, SessionLocal
from .models import User, Instance, SiteConfig, ImageSource
from .auth import get_current_user_from_cookie
from .docker_manager import (
    create_user_instance_async, get_creation_progress,
    stop_user_instance, start_user_instance, restart_user_instance,
    delete_user_instance, get_instance_status, get_container_logs,
    calc_ports, calc_extra_ports,
    pull_and_recreate, pull_and_recreate_single,
    get_pull_progress, get_single_pull_progress,
    get_current_progress,
    create_single_service_async, detect_public_ip,
    check_port_conflicts, get_affected_services, stop_affected_services, recreate_services,
    configure_bot_astrbot,
)
from .progress_store import TaskAlreadyRunning, get_current_task
from .image_management import resolve_image_registries, source_to_dict
from .service_access import ensure_instance_service
from .plugin_installer import get_plugin_spec, start_astrbot_plugin_install
from .instance_context import (
    get_instance, list_user_instances, instance_key, instance_to_dict,
    assert_can_create, find_available_port_base, normalize_instance_key,
    storage_root,
)
from .docker_manager import (
    create_instance_context_async, start_instance_context, stop_instance_context,
    restart_instance_context, delete_instance_context, get_instance_context_status,
    get_context_container_logs,
    recreate_instance_context_async,
    instance_container_name as instance_container_name_for_user,
)

logger = logging.getLogger(__name__)
router = APIRouter()


VALID_SERVICES = ("astrbot", "napcat", "llonebot")


def _instance_or_404(db: Session, user: User, instance_id: Optional[int] = None) -> Instance:
    row = get_instance(db, user, instance_id)
    if not row:
        raise HTTPException(status_code=404, detail="实例不存在")
    return row


def _instance_json(row: Instance, user: User) -> dict:
    try:
        status = get_instance_context_status(user.username, instance_key(row))
    except Exception:
        status = {}
    return instance_to_dict(row, status=status)


@router.get("/api/instances")
async def list_instances(user: User = Depends(get_current_user_from_cookie),
                         db: Session = Depends(get_db)):
    rows = list_user_instances(db, user)
    quota = {
        "enabled": bool(user.is_admin or user.multi_instance_enabled),
        "max_instances": max(2, int(user.max_instances or 1)) if user.is_admin else max(1, int(user.max_instances or 1)),
        "count": len(rows),
    }
    quota["remaining"] = max(0, quota["max_instances"] - quota["count"])
    quota["can_create"] = quota["count"] < quota["max_instances"] and (quota["enabled"] or quota["count"] == 0)
    return JSONResponse({"instances": [_instance_json(row, user) for row in rows], "quota": quota})


@router.post("/api/instances")
async def create_named_instance(request: Request,
                                user: User = Depends(get_current_user_from_cookie),
                                db: Session = Depends(get_db)):
    body = await request.json()
    rows = list_user_instances(db, user)
    try:
        assert_can_create(user, len(rows))
    except HTTPException as exc:
        return JSONResponse({"ok": False, "error": exc.detail}, status_code=exc.status_code)
    bot_type = body.get("bot_type", "napcat")
    if bot_type not in ("napcat", "llonebot"):
        return JSONResponse({"ok": False, "error": "bot_type 必须为 napcat 或 llonebot"}, status_code=400)
    display_name = str(body.get("display_name") or "新实例").strip()[:128]
    if not display_name:
        return JSONResponse({"ok": False, "error": "实例名称不能为空"}, status_code=400)
    if not rows:
        key = "default"
        display_name = display_name if display_name != "新实例" else "默认实例"
    else:
        base_key = normalize_instance_key(body.get("instance_key") or display_name)
        key = base_key
        existing = {instance_key(row) for row in rows}
        while key in existing:
            key = f"{base_key[:40]}-{secrets.token_hex(3)}"
    try:
        port_start = find_available_port_base(db)
    except RuntimeError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=503)
    ports = calc_ports(user.id, port_start)
    root = None if key == "default" else storage_root(Instance(user_id=user.id, instance_key=key), user.username)

    row = Instance(user_id=user.id, instance_key=key, display_name=display_name,
                   storage_root=root, port_base=port_start,
                   astrbot_port=ports["astrbot_web"], napcat_web_port=ports["napcat_web"],
                   astrbot_ws_port=ports["astrbot_ws"], extra_ports_json="[]",
                   bot_type=bot_type, status="creating")
    db.add(row); db.commit(); db.refresh(row)
    row_id = row.id

    def _cb(result, error=None):
        session = SessionLocal()
        try:
            current = session.query(Instance).filter_by(id=row_id, user_id=user.id).first()
            if not current:
                return
            if result:
                current.astrbot_container_id = result.get("astrbot_container_id")
                current.napcat_container_id = result.get("napcat_container_id") if bot_type == "napcat" else None
                current.llonebot_container_id = result.get("llonebot_container_id") if bot_type == "llonebot" else None
                current.status = "running"
            else:
                current.status = "error"
            session.commit()
        finally:
            session.close()

    try:
        task = create_instance_context_async(user.username, user.id, key, root, port_start,
                                             bot_type, _cb, body.get("image_source_id"))
    except Exception as exc:
        db.delete(row); db.commit()
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    return JSONResponse({"ok": True, "instance": instance_to_dict(row, user=user),
                         "task_id": task.get("task_id")}, status_code=202)


@router.patch("/api/instances/{instance_id}")
async def update_named_instance(instance_id: int, request: Request,
                                user: User = Depends(get_current_user_from_cookie),
                                db: Session = Depends(get_db)):
    row = _instance_or_404(db, user, instance_id)
    body = await request.json()
    if "display_name" in body:
        name = str(body.get("display_name") or "").strip()[:128]
        if not name:
            return JSONResponse({"ok": False, "error": "实例名称不能为空"}, status_code=400)
        row.display_name = name
    db.commit()
    return JSONResponse({"ok": True, "instance": _instance_json(row, user)})


@router.delete("/api/instances/{instance_id}")
async def delete_named_instance(instance_id: int,
                                user: User = Depends(get_current_user_from_cookie),
                                db: Session = Depends(get_db)):
    row = _instance_or_404(db, user, instance_id)
    rows = list_user_instances(db, user)
    try:
        delete_instance_context(user.username, instance_key(row))
    except Exception as exc:
        return JSONResponse({"ok": False, "error": f"删除容器失败：{exc}"}, status_code=500)
    was_default = instance_key(row) == "default"
    db.delete(row); db.commit()
    # 新实例的内部 key 不可变；删除旧 default 后，get_instance 会自动选择最早剩余实例。
    return JSONResponse({"ok": True})


@router.post("/api/instances/{instance_id}/start")
async def start_named_instance(instance_id: int, user: User = Depends(get_current_user_from_cookie),
                               db: Session = Depends(get_db)):
    row = _instance_or_404(db, user, instance_id)
    start_instance_context(user.username, instance_key(row)); row.status = "running"; db.commit()
    return JSONResponse({"ok": True})


@router.post("/api/instances/{instance_id}/stop")
async def stop_named_instance(instance_id: int, user: User = Depends(get_current_user_from_cookie),
                              db: Session = Depends(get_db)):
    row = _instance_or_404(db, user, instance_id)
    stop_instance_context(user.username, instance_key(row)); row.status = "stopped"; db.commit()
    return JSONResponse({"ok": True})


@router.post("/api/instances/{instance_id}/restart")
async def restart_named_instance(instance_id: int, user: User = Depends(get_current_user_from_cookie),
                                 db: Session = Depends(get_db)):
    row = _instance_or_404(db, user, instance_id)
    restart_instance_context(user.username, instance_key(row)); row.status = "running"; db.commit()
    return JSONResponse({"ok": True})


@router.post("/api/instances/{instance_id}/update")
async def update_named_instance_image(instance_id: int, request: Request,
                                      user: User = Depends(get_current_user_from_cookie),
                                      db: Session = Depends(get_db)):
    row = _instance_or_404(db, user, instance_id)
    try:
        body = await request.json()
    except Exception:
        body = {}
    try:
        extra_ports = json.loads(row.extra_ports_json or "[]")
    except Exception:
        extra_ports = []
    if instance_key(row) == "default":
        task = pull_and_recreate(user.username, user.id, extra_ports,
                                 bot_type=row.bot_type or "napcat",
                                 image_source_id=body.get("image_source_id"))
    else:
        task = recreate_instance_context_async(user.username, user.id, instance_key(row),
                                               row.storage_root, row.port_base,
                                               row.bot_type or "napcat",
                                               body.get("image_source_id"), extra_ports=extra_ports)
    return JSONResponse({"ok": True, "task_id": task["task_id"]})


@router.get("/api/instances/{instance_id}/logs/{service}")
async def named_instance_logs(instance_id: int, service: str, lines: int = 200,
                               user: User = Depends(get_current_user_from_cookie),
                               db: Session = Depends(get_db)):
    if service not in VALID_SERVICES:
        raise HTTPException(400, "无效服务")
    row = _instance_or_404(db, user, instance_id)
    ensure_instance_service(user, service, row)
    lines = max(20, min(lines, 1000))
    logs = (get_container_logs(user.username, service, lines) if instance_key(row) == "default"
            else get_context_container_logs(user.username, instance_key(row), service, lines))
    return JSONResponse({"ok": True, "logs": logs})


@router.post("/api/instances/{instance_id}/services/{service}")
async def create_named_service(instance_id: int, service: str,
                               request: Request,
                               user: User = Depends(get_current_user_from_cookie),
                               db: Session = Depends(get_db)):
    if service not in VALID_SERVICES:
        raise HTTPException(400, "无效服务")
    row = _instance_or_404(db, user, instance_id)
    if service in ("napcat", "llonebot"):
        row.bot_type = service
    try:
        body = await request.json()
    except Exception:
        body = {}
    try:
        extra_ports = json.loads(row.extra_ports_json or "[]")
    except Exception:
        extra_ports = []
    if instance_key(row) == "default":
        task = create_single_service_async(user.username, user.id, service, extra_ports,
                                           image_source_id=body.get("image_source_id"))
    else:
        task = create_instance_context_async(user.username, user.id, instance_key(row),
                                             row.storage_root, row.port_base,
                                             row.bot_type or "napcat", lambda *_: None,
                                             body.get("image_source_id"), extra_ports=extra_ports)
    db.commit()
    return JSONResponse({"ok": True, "task_id": task["task_id"]}, status_code=202)


@router.post("/api/instances/{instance_id}/plugins/{plugin_key}")
async def install_named_plugin(instance_id: int, plugin_key: str,
                               user: User = Depends(get_current_user_from_cookie),
                               db: Session = Depends(get_db)):
    access = _tool_access(db, "quick_tool_install_astrbot_plugin_access", "limited")
    if not _tool_allowed(access, user):
        return JSONResponse({"ok": False, "error": "该快捷工具仅限 VIP 用户使用"}, status_code=403)
    row = _instance_or_404(db, user, instance_id)
    try:
        spec = get_plugin_spec(plugin_key)
        status = (get_instance_status(user.username) if instance_key(row) == "default" else
                  get_instance_context_status(user.username, instance_key(row)))
        if status.get("astrbot") != "running":
            return JSONResponse({"ok": False, "error": "AstrBot 容器需要处于 running 状态"}, status_code=409)
        task = start_astrbot_plugin_install(user.username, spec.key, instance_key(row))
        return JSONResponse({"ok": True, "task_id": task["task_id"], "plugin": spec.display_name})
    except TaskAlreadyRunning as exc:
        return JSONResponse({"ok": False, "error": str(exc), "task": exc.task}, status_code=409)
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)


def _is_vip_user(user: User) -> bool:
    if user.is_admin:
        return True
    return bool(user.vip_expire_at and user.vip_expire_at >= datetime.now())


def _tool_access(db: Session, key: str, default: str = "limited") -> str:
    cfg = db.query(SiteConfig).filter_by(key=key).first()
    value = (cfg.value if cfg else default or "limited").strip().lower()
    if value not in ("free", "limited", "vip"):
        value = default
    return value


def _tool_allowed(access: str, user: User) -> bool:
    return access != "vip" or _is_vip_user(user)


# ════════════════════════════════════════════════════════════
#  实例管理 API
# ════════════════════════════════════════════════════════════
@router.post("/api/instance/create")
async def create_instance(user: User = Depends(get_current_user_from_cookie),
                          db: Session = Depends(get_db),
                          bot_type: str = Body("napcat", embed=True),
                          image_source_id: Optional[int] = Body(None, embed=True)):
    if db.query(Instance).filter_by(user_id=user.id).first():
        return JSONResponse({"error": "实例已存在"})

    if bot_type not in ("napcat", "llonebot"):
        bot_type = "napcat"

    ports = calc_ports(user.id)

    def _cb(result, error=None):
        _db = SessionLocal()
        try:
            inst = _db.query(Instance).filter_by(user_id=user.id).first()
            if result:
                inst.astrbot_container_id = result["astrbot_container_id"]
                if bot_type == "llonebot":
                    inst.llonebot_container_id = result.get("llonebot_container_id")
                    inst.napcat_container_id = None
                else:
                    inst.napcat_container_id = result.get("napcat_container_id")
                    inst.llonebot_container_id = None
                inst.bot_type = bot_type
                inst.status = "running"
            else:
                inst.status = "error"
            _db.commit()
        finally:
            _db.close()

    inst = Instance(
        user_id=user.id,
        instance_key="default", display_name="默认实例",
        storage_root=storage_root(Instance(user_id=user.id, instance_key="default"), user.username),
        port_base=ports["astrbot_web"],
        astrbot_port=ports["astrbot_web"],
        napcat_web_port=ports["napcat_web"],
        astrbot_ws_port=ports["astrbot_ws"],
        extra_ports_json="[]",
        bot_type=bot_type,
        status="creating",
    )
    db.add(inst); db.commit()
    try:
        task = create_user_instance_async(user.username, user.id, _cb, bot_type=bot_type,
                                          image_source_id=image_source_id)
    except TaskAlreadyRunning as exc:
        db.delete(inst); db.commit()
        return JSONResponse({"ok": False, "error": str(exc), "task": exc.task}, status_code=409)
    except ValueError as exc:
        db.delete(inst); db.commit()
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    return JSONResponse({"ok": True, "task_id": task["task_id"]})


@router.get("/api/instance/progress")
async def instance_progress(user: User = Depends(get_current_user_from_cookie),
                            instance_id: int | None = Query(None), db: Session = Depends(get_db)):
    inst = get_instance(db, user, instance_id)
    return JSONResponse(get_current_task(user.username, instance_key=instance_key(inst) if inst else "default"))


@router.post("/api/instance/start")
async def start_instance(user: User = Depends(get_current_user_from_cookie),
                         db: Session = Depends(get_db), instance_id: int | None = Query(None)):
    inst = _instance_or_404(db, user, instance_id)
    if not inst: raise HTTPException(404)
    (start_user_instance(user.username) if instance_key(inst) == "default" else
     start_instance_context(user.username, instance_key(inst)))
    inst.status = "running"; db.commit()
    return RedirectResponse("/dashboard", status_code=302)


@router.post("/api/instance/stop")
async def stop_instance(user: User = Depends(get_current_user_from_cookie),
                        db: Session = Depends(get_db), instance_id: int | None = Query(None)):
    inst = _instance_or_404(db, user, instance_id)
    if not inst: raise HTTPException(404)
    (stop_user_instance(user.username) if instance_key(inst) == "default" else
     stop_instance_context(user.username, instance_key(inst)))
    inst.status = "stopped"; db.commit()
    return RedirectResponse("/dashboard", status_code=302)


@router.post("/api/instance/restart")
async def restart_instance(user: User = Depends(get_current_user_from_cookie),
                           db: Session = Depends(get_db), instance_id: int | None = Query(None)):
    inst = _instance_or_404(db, user, instance_id)
    if not inst: raise HTTPException(404)
    (restart_user_instance(user.username) if instance_key(inst) == "default" else
     restart_instance_context(user.username, instance_key(inst)))
    return RedirectResponse("/dashboard", status_code=302)


@router.post("/api/instance/start/{service}")
async def start_single(service: str,
                       user: User = Depends(get_current_user_from_cookie),
                       db: Session = Depends(get_db), instance_id: int | None = Query(None)):
    if service not in VALID_SERVICES: raise HTTPException(400)
    inst = _instance_or_404(db, user, instance_id)
    if not inst: raise HTTPException(404)
    (start_user_instance(user.username, service) if instance_key(inst) == "default" else
     start_instance_context(user.username, instance_key(inst), service))
    return RedirectResponse("/dashboard", status_code=302)


@router.post("/api/instance/stop/{service}")
async def stop_single(service: str,
                      user: User = Depends(get_current_user_from_cookie),
                      db: Session = Depends(get_db), instance_id: int | None = Query(None)):
    if service not in VALID_SERVICES: raise HTTPException(400)
    inst = _instance_or_404(db, user, instance_id)
    if not inst: raise HTTPException(404)
    (stop_user_instance(user.username, service) if instance_key(inst) == "default" else
     stop_instance_context(user.username, instance_key(inst), service))
    return RedirectResponse("/dashboard", status_code=302)


@router.post("/api/instance/restart/{service}")
async def restart_single(service: str,
                         user: User = Depends(get_current_user_from_cookie),
                         db: Session = Depends(get_db), instance_id: int | None = Query(None)):
    if service not in VALID_SERVICES: raise HTTPException(400)
    inst = _instance_or_404(db, user, instance_id)
    if not inst: raise HTTPException(404)
    (restart_user_instance(user.username, service) if instance_key(inst) == "default" else
     restart_instance_context(user.username, instance_key(inst), service))
    return RedirectResponse("/dashboard", status_code=302)


@router.post("/api/instance/create/{service}")
async def create_single(service: str,
                        user: User = Depends(get_current_user_from_cookie),
                        db: Session = Depends(get_db),
                        image_source_id: Optional[int] = Body(None, embed=True),
                        instance_id: int | None = Query(None)):
    if service not in VALID_SERVICES: raise HTTPException(400)

    inst = get_instance(db, user, instance_id)
    active_task = get_current_task(user.username, instance_key=instance_key(inst) if inst else "default")
    if active_task.get("running"):
        return JSONResponse({"ok": False, "error": "已有后台任务正在执行",
                             "task": active_task}, status_code=409)
    try:
        resolve_image_registries(image_source_id)
    except ValueError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    if not inst:
        ports = calc_ports(user.id)
        inst = Instance(
            user_id=user.id,
            instance_key="default", display_name="默认实例",
            storage_root=storage_root(Instance(user_id=user.id, instance_key="default"), user.username),
            port_base=ports["astrbot_web"],
            astrbot_port=ports["astrbot_web"],
            napcat_web_port=ports["napcat_web"],
            astrbot_ws_port=ports["astrbot_ws"],
            extra_ports_json="[]",
            bot_type="llonebot" if service == "llonebot" else "napcat",
            status="creating",
        )
        db.add(inst); db.commit()
    else:
        # 更新 bot_type
        if service in ("napcat", "llonebot"):
            inst.bot_type = service
            extra_ports = json.loads(inst.extra_ports_json or "[]")
            extra_ports = [ep for ep in extra_ports if ep.get("service") in ("astrbot", service)]
            inst.extra_ports_json = json.dumps(extra_ports)
            db.commit()

    extra_ports = json.loads(inst.extra_ports_json or "[]")
    try:
        if instance_key(inst) == "default":
            task = create_single_service_async(user.username, user.id, service, extra_ports,
                                               image_source_id=image_source_id)
        else:
            task = create_instance_context_async(user.username, user.id, instance_key(inst),
                                                 inst.storage_root, inst.port_base,
                                                 inst.bot_type or "napcat", lambda *_: None,
                                                 image_source_id, extra_ports=extra_ports)
    except TaskAlreadyRunning as exc:
        return JSONResponse({"ok": False, "error": str(exc), "task": exc.task}, status_code=409)
    except ValueError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    return JSONResponse({"ok": True, "task_id": task["task_id"]})


@router.post("/api/instance/delete/{service}")
async def delete_single(service: str,
                        user: User = Depends(get_current_user_from_cookie),
                        db: Session = Depends(get_db), instance_id: int | None = Query(None)):
    if service not in VALID_SERVICES: raise HTTPException(400)
    inst = _instance_or_404(db, user, instance_id)
    if not inst: raise HTTPException(404)
    (delete_user_instance(user.username, service) if instance_key(inst) == "default" else
     delete_instance_context(user.username, instance_key(inst), service))

    # 删除单个容器后，检查是否所有容器均已不存在
    status = (get_instance_status(user.username) if instance_key(inst) == "default" else
              get_instance_context_status(user.username, instance_key(inst)))
    all_gone = all(status.get(k, "not_found") == "not_found"
                   for k in ("astrbot", "napcat", "llonebot"))
    if all_gone:
        db.delete(inst); db.commit()
    else:
        # 更新对应的 container_id 为 None
        if service == "astrbot":
            inst.astrbot_container_id = None
        elif service == "napcat":
            inst.napcat_container_id = None
        elif service == "llonebot":
            inst.llonebot_container_id = None
        db.commit()
    return RedirectResponse("/dashboard", status_code=302)


@router.post("/api/instance/delete")
async def delete_instance(user: User = Depends(get_current_user_from_cookie),
                          db: Session = Depends(get_db), instance_id: int | None = Query(None)):
    inst = _instance_or_404(db, user, instance_id)
    if not inst: raise HTTPException(404)
    (delete_user_instance(user.username) if instance_key(inst) == "default" else
     delete_instance_context(user.username, instance_key(inst)))
    db.delete(inst); db.commit()
    return RedirectResponse("/dashboard", status_code=302)


@router.post("/api/instance/update")
async def update_instance(user: User = Depends(get_current_user_from_cookie),
                          db: Session = Depends(get_db),
                          image_source_id: Optional[int] = Body(None, embed=True),
                          instance_id: int | None = Query(None)):
    inst = _instance_or_404(db, user, instance_id)
    if not inst: return JSONResponse({"error": "实例不存在"})
    active_task = get_current_task(user.username, instance_key=instance_key(inst))
    if active_task.get("running"):
        return JSONResponse({"ok": False, "error": "已有后台任务正在执行",
                             "task": active_task}, status_code=409)
    try:
        resolve_image_registries(image_source_id)
    except ValueError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    extra_ports = json.loads(inst.extra_ports_json or "[]")
    try:
        if instance_key(inst) == "default":
            task = pull_and_recreate(user.username, user.id, extra_ports,
                                     bot_type=inst.bot_type or "napcat",
                                     image_source_id=image_source_id)
        else:
            task = recreate_instance_context_async(user.username, user.id, instance_key(inst),
                                                   inst.storage_root, inst.port_base,
                                                   inst.bot_type or "napcat", image_source_id,
                                                   extra_ports=extra_ports)
    except TaskAlreadyRunning as exc:
        return JSONResponse({"ok": False, "error": str(exc), "task": exc.task}, status_code=409)
    except ValueError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    return JSONResponse({"ok": True, "task_id": task["task_id"]})


@router.get("/api/instance/update_progress")
async def update_progress(user: User = Depends(get_current_user_from_cookie),
                          instance_id: int | None = Query(None), db: Session = Depends(get_db)):
    inst = get_instance(db, user, instance_id)
    return JSONResponse(get_pull_progress(user.username, instance_key(inst) if inst else "default"))


@router.get("/api/instance/update_progress/current")
async def current_update_progress(user: User = Depends(get_current_user_from_cookie),
                                  instance_id: int | None = Query(None), db: Session = Depends(get_db)):
    inst = get_instance(db, user, instance_id)
    return JSONResponse(get_current_task(user.username, instance_key=instance_key(inst) if inst else "default"))


@router.post("/api/instance/update/{service}")
async def update_single(service: str,
                        user: User = Depends(get_current_user_from_cookie),
                        db: Session = Depends(get_db),
                        image_source_id: Optional[int] = Body(None, embed=True),
                        instance_id: int | None = Query(None)):
    if service not in VALID_SERVICES:
        raise HTTPException(400, "无效服务")
    inst = get_instance(db, user, instance_id)
    if not inst: return JSONResponse({"error": "实例不存在"})
    active_task = get_current_task(user.username, instance_key=instance_key(inst))
    if active_task.get("running"):
        return JSONResponse({"ok": False, "error": "已有后台任务正在执行",
                             "task": active_task}, status_code=409)
    try:
        resolve_image_registries(image_source_id)
    except ValueError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    extra_ports = json.loads(inst.extra_ports_json or "[]")
    if service in ("napcat", "llonebot"):
        inst.bot_type = service
        inst.napcat_container_id = None
        inst.llonebot_container_id = None
        extra_ports = [ep for ep in extra_ports if ep.get("service") in ("astrbot", service)]
        inst.extra_ports_json = json.dumps(extra_ports)
        db.commit()
    try:
        if instance_key(inst) == "default":
            task = pull_and_recreate_single(user.username, user.id, service, extra_ports,
                                            image_source_id=image_source_id)
        else:
            task = recreate_instance_context_async(user.username, user.id, instance_key(inst),
                                                   inst.storage_root, inst.port_base,
                                                   inst.bot_type or "napcat", image_source_id,
                                                   extra_ports=extra_ports)
    except TaskAlreadyRunning as exc:
        return JSONResponse({"ok": False, "error": str(exc), "task": exc.task}, status_code=409)
    except ValueError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    return JSONResponse({"ok": True, "task_id": task["task_id"]})


@router.get("/api/instance/update_progress/{service}")
async def update_single_progress(service: str,
                                  user: User = Depends(get_current_user_from_cookie),
                                  instance_id: int | None = Query(None), db: Session = Depends(get_db)):
    inst = get_instance(db, user, instance_id)
    return JSONResponse(get_single_pull_progress(user.username, service,
                                                 instance_key(inst) if inst else "default"))


@router.get("/api/instance/image_sources")
async def instance_image_sources(user: User = Depends(get_current_user_from_cookie),
                                 db: Session = Depends(get_db)):
    sources = db.query(ImageSource).filter_by(enabled=True).order_by(
        ImageSource.priority.asc(), ImageSource.id.asc()
    ).all()
    sources = sorted(sources, key=lambda item: (not item.is_default, item.priority, item.id))
    return JSONResponse({"sources": [source_to_dict(item) for item in sources]})


@router.post("/api/instance/auto_config")
async def auto_config_instance(user: User = Depends(get_current_user_from_cookie),
                               db: Session = Depends(get_db), instance_id: int | None = Query(None)):
    access = _tool_access(db, "quick_tool_auto_config_access", "limited")
    if not _tool_allowed(access, user):
        return JSONResponse({"ok": False, "error": "该快捷工具仅限 VIP 用户使用"}, status_code=403)
    inst = _instance_or_404(db, user, instance_id)
    if not inst:
        return JSONResponse({"ok": False, "error": "实例不存在"})
    bot_type = inst.bot_type or "napcat"

    ws_port = inst.astrbot_ws_port
    try:
        extra_ports = json.loads(inst.extra_ports_json or "[]")
    except Exception:
        extra_ports = []
    for ep in extra_ports:
        if ep.get("service") == "astrbot" and ep.get("container_port") == 6199:
            ws_port = ep.get("host_port", ws_port)

    public_host = (detect_public_ip() or "").replace("https://", "").replace("http://", "").split("/")[0]
    ws_url = f"ws://{public_host}:{ws_port}/ws"
    return JSONResponse(configure_bot_astrbot(user.username, bot_type, ws_url, instance_key(inst)))


# 弹性端口
@router.get("/api/instance/extra_ports")
async def get_extra_ports(user: User = Depends(get_current_user_from_cookie),
                          db: Session = Depends(get_db), instance_id: int | None = Query(None)):
    inst = _instance_or_404(db, user, instance_id)
    if not inst: return JSONResponse({"mappings": [], "available": []})
    mappings = json.loads(inst.extra_ports_json or "[]")
    used = {m["host_port"] for m in mappings}
    available = [p for p in calc_extra_ports(user.id, inst.port_base) if p not in used]
    return JSONResponse({"mappings": mappings, "available": available})


@router.post("/api/instance/extra_ports")
async def save_extra_ports(request: Request,
                           user: User = Depends(get_current_user_from_cookie),
                           db: Session = Depends(get_db), instance_id: int | None = Query(None)):
    inst = _instance_or_404(db, user, instance_id)
    if not inst:
        return JSONResponse({"error": "实例不存在"})

    body = await request.json()
    new_mappings = body.get("mappings", [])
    allowed = set(calc_extra_ports(user.id, inst.port_base))

    for m in new_mappings:
        if m.get("host_port") not in allowed:
            return JSONResponse({"error": f"端口 {m.get('host_port')} 不在允许范围内"})

    try:
        old_mappings = json.loads(inst.extra_ports_json or "[]")
    except Exception:
        old_mappings = []

    affected = get_affected_services(old_mappings, new_mappings)

    if not affected:
        inst.extra_ports_json = json.dumps(new_mappings)
        db.commit()
        return JSONResponse({"ok": True, "rebuilt": False,
                             "message": "配置未变化，已保存（无需重建容器）"})

    active_task = get_current_task(user.username, instance_key=instance_key(inst))
    if active_task.get("running"):
        return JSONResponse({"ok": False, "error": "已有后台任务正在执行",
                             "task": active_task}, status_code=409)

    if instance_key(inst) != "default":
        inst.extra_ports_json = json.dumps(new_mappings)
        db.commit()
        task = recreate_instance_context_async(
            user.username, user.id, instance_key(inst), inst.storage_root,
            inst.port_base, inst.bot_type or "napcat", extra_ports=new_mappings,
            force_pull=False,
        )
        return JSONResponse({"ok": True, "rebuilt": True, "affected": affected,
                             "task_id": task["task_id"]})

    # 检查新端口是否被非重建容器占用（排除即将重建的容器）
    new_host_ports = [m["host_port"] for m in new_mappings
                      if m.get("service") in affected]
    exclude_containers = [instance_container_name_for_user(user.username, svc, instance_key(inst)) for svc in affected]
    conflicts = check_port_conflicts(new_host_ports, exclude_containers)
    if conflicts:
        detail = "；".join(f"端口 {p} 被容器 {c} 占用" for p, c in conflicts.items())
        return JSONResponse({"error": f"端口冲突，无法重建：{detail}"})

    # 先同步停止受影响容器（释放端口），再提交 DB，最后异步启动
    # 顺序保证：端口释放 → 配置持久化 → 新容器启动，避免交叉占用导致冲突
    try:
        stop_affected_services(user.username, affected)
    except Exception as e:
        return JSONResponse({"error": f"停止旧容器失败：{e}"})

    inst.extra_ports_json = json.dumps(new_mappings)
    db.commit()
    try:
        task = recreate_services(user.username, user.id, affected, new_mappings, old_mappings)
    except TaskAlreadyRunning as exc:
        return JSONResponse({"ok": False, "error": str(exc), "task": exc.task}, status_code=409)
    return JSONResponse({"ok": True, "rebuilt": True, "affected": affected,
                         "task_id": task["task_id"]})


# ════════════════════════════════════════════════════════════
#  日志
# ════════════════════════════════════════════════════════════
@router.get("/api/instance/napcat_token")
async def get_napcat_token(user: User = Depends(get_current_user_from_cookie),
                         db: Session = Depends(get_db), instance_id: int | None = Query(None)):
    inst = _instance_or_404(db, user, instance_id)
    bot_type = inst.bot_type if inst else "napcat"
    service_name = "llonebot" if bot_type == "llonebot" else "napcat"
    container_name = instance_container_name_for_user(user.username, service_name, instance_key(inst))
    try:
        client = docker_lib.from_env()
        container = client.containers.get(container_name)
        if bot_type == "llonebot":
            result = container.exec_run([
                "sh", "-lc",
                "for p in /root/llonebot/data/auth_token.txt /root/llonebot/data/webui_token.txt; "
                "do [ -s \"$p\" ] && cat \"$p\" && exit 0; done; exit 1",
            ])
            if result.exit_code == 0:
                token = result.output.decode("utf-8", errors="ignore").strip()
                if token:
                    return JSONResponse({"token": token})
        logs = container.logs(stream=False, timestamps=False)
        text = logs.decode("utf-8", errors="replace")
        matches = re.findall(r'WebUi Token:\s*([a-f0-9]+)', text, re.IGNORECASE)
        if matches:
            return JSONResponse({"token": matches[-1]})
    except Exception as e:
        logger.error(f"获取 {bot_type} token 失败: {e}")
    return JSONResponse({"token": None})


@router.get("/logs/{service}", response_class=HTMLResponse)
async def view_logs(service: str, request: Request,
                    instance_id: int | None = Query(None),
                    user: User = Depends(get_current_user_from_cookie),
                    db: Session = Depends(get_db)):
    inst = _instance_or_404(db, user, instance_id)
    ensure_instance_service(user, service, inst)
    logs = (get_container_logs(user.username, service, 200) if instance_key(inst) == "default"
            else get_context_container_logs(user.username, instance_key(inst), service, 200))
    return templates.TemplateResponse(request, "logs.html",
        {"user": user, "service": service, "logs": logs})


@router.get("/api/logs/{service}")
async def api_logs(service: str, lines: int = 200,
                   instance_id: int | None = Query(None),
                   user: User = Depends(get_current_user_from_cookie),
                   db: Session = Depends(get_db)):
    inst = _instance_or_404(db, user, instance_id)
    ensure_instance_service(user, service, inst)
    lines = max(20, min(lines, 1000))
    logs = (get_container_logs(user.username, service, lines) if instance_key(inst) == "default"
            else get_context_container_logs(user.username, instance_key(inst), service, lines))
    return JSONResponse({"ok": True, "logs": logs})
