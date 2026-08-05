import json
import re
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy.orm import Session

from .auth import get_current_user_from_cookie
from .bootstrap import templates
from .database import get_db
from .image_management import (
    normalize_registry,
    run_cleanup,
    source_to_dict,
)
from .models import ImageCleanupRun, ImageSource, SiteConfig, User


router = APIRouter()


def require_admin(user: User = Depends(get_current_user_from_cookie)) -> User:
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="权限不足")
    return user


def _site_value(db: Session, key: str, default: str) -> str:
    row = db.query(SiteConfig).filter_by(key=key).first()
    return row.value if row else default


def _set_site_value(db: Session, key: str, value: str) -> None:
    row = db.query(SiteConfig).filter_by(key=key).first()
    if row:
        row.value = value
    else:
        db.add(SiteConfig(key=key, value=value))


def _cleanup_run_to_dict(run: Optional[ImageCleanupRun]) -> dict:
    if not run:
        return {}
    try:
        summary = json.loads(run.summary_json or "{}")
    except json.JSONDecodeError:
        summary = {}
    return {
        "id": run.id,
        "status": run.status,
        "dry_run": bool(run.dry_run),
        "summary": summary,
        "error": run.error or "",
        "started_at": run.started_at.isoformat() if run.started_at else None,
        "finished_at": run.finished_at.isoformat() if run.finished_at else None,
    }


@router.get("/admin/images", response_class=HTMLResponse)
async def admin_images_page(request: Request, user: User = Depends(require_admin),
                            db: Session = Depends(get_db)):
    sources = db.query(ImageSource).order_by(ImageSource.priority, ImageSource.id).all()
    latest = db.query(ImageCleanupRun).order_by(ImageCleanupRun.started_at.desc()).first()
    return templates.TemplateResponse("admin_images.html", {
        "request": request,
        "user": user,
        "sources": sources,
        "cleanup_enabled": _site_value(db, "image_cleanup_enabled", "false") == "true",
        "cleanup_time": _site_value(db, "image_cleanup_time", "03:30"),
        "latest_cleanup": _cleanup_run_to_dict(latest),
    })


@router.get("/api/admin/image_sources")
async def admin_list_image_sources(user: User = Depends(require_admin),
                                   db: Session = Depends(get_db)):
    sources = db.query(ImageSource).order_by(ImageSource.priority, ImageSource.id).all()
    return {"sources": [source_to_dict(item) for item in sources]}


@router.post("/api/admin/image_sources")
async def admin_create_image_source(request: Request, user: User = Depends(require_admin),
                                    db: Session = Depends(get_db)):
    body = await request.json()
    name = str(body.get("name", "")).strip()
    if not name:
        raise HTTPException(400, "镜像源名称不能为空")
    try:
        registry = normalize_registry(str(body.get("registry", "")))
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    if db.query(ImageSource).filter_by(registry=registry).first():
        raise HTTPException(409, "镜像源地址已存在")
    max_priority = max([item.priority for item in db.query(ImageSource).all()] or [-1])
    source = ImageSource(name=name[:64], registry=registry, enabled=True,
                         priority=max_priority + 1, is_default=False, is_official=False)
    db.add(source); db.commit(); db.refresh(source)
    return source_to_dict(source)


@router.put("/api/admin/image_sources/{source_id}")
async def admin_update_image_source(source_id: int, request: Request,
                                    user: User = Depends(require_admin),
                                    db: Session = Depends(get_db)):
    source = db.query(ImageSource).filter_by(id=source_id).first()
    if not source:
        raise HTTPException(404, "镜像源不存在")
    body = await request.json()
    if "name" in body:
        name = str(body["name"]).strip()
        if not name:
            raise HTTPException(400, "镜像源名称不能为空")
        source.name = name[:64]
    if "registry" in body and not source.is_official:
        try:
            registry = normalize_registry(str(body["registry"]))
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        duplicate = db.query(ImageSource).filter(
            ImageSource.registry == registry, ImageSource.id != source.id
        ).first()
        if duplicate:
            raise HTTPException(409, "镜像源地址已存在")
        source.registry = registry
    if "enabled" in body:
        source.enabled = True if source.is_official else bool(body["enabled"])
        if not source.enabled and source.is_default:
            source.is_default = False
            official = db.query(ImageSource).filter_by(is_official=True).first()
            if official:
                official.is_default = True
                official.enabled = True
    if body.get("is_default"):
        db.query(ImageSource).update({ImageSource.is_default: False})
        source.is_default = True
        source.enabled = True
    db.commit(); db.refresh(source)
    return source_to_dict(source)


@router.delete("/api/admin/image_sources/{source_id}")
async def admin_delete_image_source(source_id: int, user: User = Depends(require_admin),
                                    db: Session = Depends(get_db)):
    source = db.query(ImageSource).filter_by(id=source_id).first()
    if not source:
        raise HTTPException(404, "镜像源不存在")
    if source.is_official:
        raise HTTPException(400, "官方 Docker Hub 源不能删除")
    was_default = source.is_default
    db.delete(source)
    if was_default:
        official = db.query(ImageSource).filter_by(is_official=True).first()
        if official:
            official.is_default = True
            official.enabled = True
    db.commit()
    return {"ok": True}


@router.post("/api/admin/image_sources/reorder")
async def admin_reorder_image_sources(request: Request, user: User = Depends(require_admin),
                                      db: Session = Depends(get_db)):
    body = await request.json()
    ids = body.get("ids", [])
    if not isinstance(ids, list) or any(not isinstance(value, int) for value in ids):
        raise HTTPException(400, "排序数据无效")
    sources = {item.id: item for item in db.query(ImageSource).all()}
    if set(ids) != set(sources):
        raise HTTPException(400, "排序必须包含全部镜像源")
    for priority, source_id in enumerate(ids):
        sources[source_id].priority = priority
    db.commit()
    return {"ok": True}


@router.post("/api/admin/images/cleanup/settings")
async def admin_cleanup_settings(request: Request, user: User = Depends(require_admin),
                                 db: Session = Depends(get_db)):
    body = await request.json()
    schedule = str(body.get("time", "03:30"))
    if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", schedule):
        raise HTTPException(400, "执行时间格式应为 HH:MM")
    _set_site_value(db, "image_cleanup_enabled", "true" if body.get("enabled") else "false")
    _set_site_value(db, "image_cleanup_time", schedule)
    db.commit()
    return {"ok": True, "enabled": bool(body.get("enabled")), "time": schedule}


@router.post("/api/admin/images/cleanup/preview")
async def admin_cleanup_preview(user: User = Depends(require_admin)):
    try:
        return JSONResponse(run_cleanup(dry_run=True))
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


@router.post("/api/admin/images/cleanup/run")
async def admin_cleanup_run(user: User = Depends(require_admin)):
    try:
        return JSONResponse({"ok": True, **run_cleanup(dry_run=False)})
    except RuntimeError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=409)
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


@router.get("/api/admin/images/cleanup/runs")
async def admin_cleanup_runs(user: User = Depends(require_admin),
                             db: Session = Depends(get_db)):
    runs = db.query(ImageCleanupRun).order_by(ImageCleanupRun.started_at.desc()).limit(20).all()
    return {"runs": [_cleanup_run_to_dict(run) for run in runs]}
