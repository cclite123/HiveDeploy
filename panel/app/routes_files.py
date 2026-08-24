import json
import logging

from fastapi import APIRouter, Request, Depends, Form, HTTPException, UploadFile, File, Query
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse, StreamingResponse
from sqlalchemy.orm import Session

from .bootstrap import templates
from .database import get_db
from .models import User
from .auth import get_current_user_from_cookie
from .service_access import ensure_instance_service
from .instance_context import get_instance, instance_key
from .filemanager import (
    list_dir, read_file, write_file, delete_path, make_dir,
    get_shortcuts, get_root, is_text_file,
    download_file, upload_file,
    move_path, copy_path, rename_path,
    extract_archive, compress_path, get_file_info,
    path_exists,
)

logger = logging.getLogger(__name__)
router = APIRouter()


def _file_scope(user: User, service: str, instance_id: int | None, db: Session):
    instance = get_instance(db, user, instance_id)
    if not instance:
        raise HTTPException(404, "实例不存在")
    ensure_instance_service(user, service, instance)
    return f"{user.username}::{instance_key(instance)}", instance


# ════════════════════════════════════════════════════════════
#  文件管理
# ════════════════════════════════════════════════════════════
@router.get("/files/{service}", response_class=HTMLResponse)
async def files_page(service: str, request: Request, path: str = None,
                     saved: bool = False,
                     instance_id: int | None = Query(None),
                     user: User = Depends(get_current_user_from_cookie),
                     db: Session = Depends(get_db)):
    scoped_username, instance = _file_scope(user, service, instance_id, db)
    root      = get_root(service)
    path      = path or root
    if service == "napcat" and path == "/app/config":
        path = "/app/napcat/config"
    if service == "llonebot" and path.startswith("/app"):
        path = root
    shortcuts = get_shortcuts(service)
    astrbot_root  = get_root("astrbot")
    napcat_root   = get_root("napcat")
    llonebot_root = get_root("llonebot")

    error_msg = ""
    is_file   = False
    entries   = []
    file_content = ""
    filename  = ""
    parent_path = None

    ptype = "dir"
    try:
        ptype = path_exists(scoped_username, service, path)
    except Exception:
        pass

    if ptype == "file":
        is_file  = True
        filename = path.split("/")[-1]
        parent_path = "/".join(path.split("/")[:-1]) or "/"
        if is_text_file(filename):
            result = read_file(scoped_username, service, path)
            file_content = result["content"]
            error_msg    = result["error"]
        else:
            error_msg = "该文件为二进制文件，无法在线编辑。请下载后修改。"
            file_content = None
    else:
        parent_path = "/".join(path.split("/")[:-1]) or None
        if path == "/" or path == root:
            parent_path = None
        result  = list_dir(scoped_username, service, path)
        entries = result["entries"]
        error_msg = result["error"]

    TEXT_EXTS_JS = (".txt", ".json", ".yaml", ".yml", ".toml", ".ini", ".cfg",
        ".conf", ".py", ".js", ".ts", ".md", ".html", ".css",
        ".sh", ".bash", ".env", ".log", ".xml", ".csv", ".properties",
        ".rst", ".jsx", ".tsx", ".vue", ".sql")
    IMG_EXTS_JS  = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".bmp")
    VID_EXTS_JS  = (".mp4", ".webm", ".mov", ".mkv")
    AUD_EXTS_JS  = (".mp3", ".wav", ".ogg", ".flac", ".aac", ".m4a")
    ARC_EXTS_JS  = (".zip", ".tar", ".gz", ".tgz", ".bz2")
    entries_json = json.dumps([
        {
            "path": e["full_path"],
            "name": e["name"],
            "isdir": e["is_dir"],
            "size": int(e.get("size", 0)) if e.get("size", "0").isdigit() else 0,
            "mtime": e.get("mtime", 0),
            "isImg": e["name"].lower().endswith(IMG_EXTS_JS),
            "isVid": e["name"].lower().endswith(VID_EXTS_JS),
            "isAud": e["name"].lower().endswith(AUD_EXTS_JS),
            "isArc": e["name"].lower().endswith(ARC_EXTS_JS),
        }
        for e in entries
    ]) if not is_file else "[]"
    return templates.TemplateResponse(request, "files.html", {"user": user, "active_instance": instance, "service": service,
        "current_path": path, "parent_path": parent_path,
        "shortcuts": shortcuts, "entries": entries, "is_file": is_file,
        "file_content": file_content, "filename": filename,
        "error_msg": error_msg, "saved": saved,
        "astrbot_root": astrbot_root, "napcat_root": napcat_root,
        "llonebot_root": llonebot_root,
        "entries_json": entries_json, "instance_id": instance.id,
    })


@router.post("/files/{service}/save")
async def save_file(service: str, path: str = Form(...), content: str = Form(...),
                    instance_id: int | None = Query(None),
                    user: User = Depends(get_current_user_from_cookie), db: Session = Depends(get_db)):
    scoped, _ = _file_scope(user, service, instance_id, db)
    result = write_file(scoped, service, path, content)
    if result["error"]:
        return RedirectResponse(f"/files/{service}?path={path}&error={result['error']}", 302)
    return RedirectResponse(f"/files/{service}?path={path}&saved=1", 302)


@router.post("/files/{service}/delete")
async def delete_file(service: str, path: str = Form(...),
                      return_path: str = Form("/"),
                      instance_id: int | None = Query(None),
                      user: User = Depends(get_current_user_from_cookie), db: Session = Depends(get_db)):
    scoped, _ = _file_scope(user, service, instance_id, db)
    delete_path(scoped, service, path)
    return RedirectResponse(f"/files/{service}?path={return_path}", 302)


@router.post("/files/{service}/mkdir")
async def mkdir(service: str, base_path: str = Form(...),
                dirname: str = Form(...), return_path: str = Form("/"),
                instance_id: int | None = Query(None),
                user: User = Depends(get_current_user_from_cookie), db: Session = Depends(get_db)):
    scoped, _ = _file_scope(user, service, instance_id, db)
    new_path = (base_path.rstrip("/") + "/" + dirname).replace("//", "/")
    make_dir(scoped, service, new_path)
    return RedirectResponse(f"/files/{service}?path={return_path}", 302)


@router.post("/files/{service}/create")
async def create_file(service: str, base_path: str = Form(...),
                      filename: str = Form(...), content: str = Form(""),
                      instance_id: int | None = Query(None),
                      user: User = Depends(get_current_user_from_cookie), db: Session = Depends(get_db)):
    scoped, _ = _file_scope(user, service, instance_id, db)
    name = filename.strip().replace("\\", "/").split("/")[-1]
    if not name:
        return JSONResponse({"ok": False, "error": "文件名不能为空"})
    if '"' in name or "\x00" in name:
        return JSONResponse({"ok": False, "error": "文件名包含不支持的字符"})
    new_path = (base_path.rstrip("/") + "/" + name).replace("//", "/")
    if path_exists(scoped, service, new_path) != "notfound":
        return JSONResponse({"ok": False, "error": "文件已存在"})
    result = write_file(scoped, service, new_path, content)
    return JSONResponse({"ok": not bool(result["error"]), "error": result["error"], "path": new_path})


@router.get("/files/{service}/download")
async def download(service: str, path: str,
                   instance_id: int | None = Query(None),
                   user: User = Depends(get_current_user_from_cookie), db: Session = Depends(get_db)):
    scoped, _ = _file_scope(user, service, instance_id, db)
    result = download_file(scoped, service, path)
    if result["error"]:
        raise HTTPException(400, result["error"])
    filename = result["filename"]
    data     = result["data"]
    return StreamingResponse(
        iter([data]),
        media_type="application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post("/files/{service}/upload")
async def upload(service: str, upload_path: str = Form(...),
                 file: UploadFile = File(...),
                 instance_id: int | None = Query(None),
                 user: User = Depends(get_current_user_from_cookie), db: Session = Depends(get_db)):
    scoped, _ = _file_scope(user, service, instance_id, db)
    data   = await file.read()
    result = upload_file(scoped, service, upload_path, file.filename, data)
    if result["error"]:
        return JSONResponse({"error": result["error"]}, status_code=400)
    return JSONResponse({"ok": True, "filename": file.filename})


@router.post("/files/{service}/move")
async def move_file(service: str, request: Request,
                    instance_id: int | None = Query(None),
                    user: User = Depends(get_current_user_from_cookie), db: Session = Depends(get_db)):
    scoped, _ = _file_scope(user, service, instance_id, db)
    body = await request.json()
    result = move_path(scoped, service, body["src"], body["dst"])
    return JSONResponse({"ok": not result["error"], "error": result["error"]})


@router.post("/files/{service}/copy")
async def copy_file(service: str, request: Request,
                    instance_id: int | None = Query(None),
                    user: User = Depends(get_current_user_from_cookie), db: Session = Depends(get_db)):
    scoped, _ = _file_scope(user, service, instance_id, db)
    body = await request.json()
    result = copy_path(scoped, service, body["src"], body["dst"])
    return JSONResponse({"ok": not result["error"], "error": result["error"]})


@router.post("/files/{service}/rename")
async def rename_file(service: str, src: str = Form(...), dst: str = Form(...),
                      instance_id: int | None = Query(None),
                      user: User = Depends(get_current_user_from_cookie), db: Session = Depends(get_db)):
    scoped, _ = _file_scope(user, service, instance_id, db)
    result = rename_path(scoped, service, src, dst)
    if result["error"]:
        return JSONResponse({"error": result["error"]}, status_code=400)
    return JSONResponse({"ok": True})


@router.post("/files/{service}/extract")
async def extract_file(service: str, path: str = Form(...),
                       dest_dir: str = Form(...),
                       instance_id: int | None = Query(None),
                       user: User = Depends(get_current_user_from_cookie), db: Session = Depends(get_db)):
    scoped, _ = _file_scope(user, service, instance_id, db)
    result = extract_archive(scoped, service, path, dest_dir)
    return JSONResponse({"ok": not result["error"], "error": result["error"]})


@router.post("/files/{service}/compress")
async def compress_file(service: str, src: str = Form(...),
                        dest_zip: str = Form(...),
                        instance_id: int | None = Query(None),
                        user: User = Depends(get_current_user_from_cookie), db: Session = Depends(get_db)):
    scoped, _ = _file_scope(user, service, instance_id, db)
    result = compress_path(scoped, service, src, dest_zip)
    return JSONResponse({"ok": not result["error"], "error": result["error"]})


@router.get("/files/{service}/preview")
async def preview_file(service: str, path: str,
                       instance_id: int | None = Query(None),
                       user: User = Depends(get_current_user_from_cookie), db: Session = Depends(get_db)):
    scoped, _ = _file_scope(user, service, instance_id, db)
    result = download_file(scoped, service, path)
    if result["error"]:
        raise HTTPException(400, result["error"])
    filename = result["filename"].lower()
    if any(filename.endswith(e) for e in (".jpg",".jpeg",".png",".gif",".webp",".svg",".bmp")):
        media_type = "image/" + (filename.rsplit(".",1)[-1].replace("jpg","jpeg"))
    elif any(filename.endswith(e) for e in (".mp4",".webm",".ogg",".mov")):
        media_type = "video/" + filename.rsplit(".",1)[-1]
    elif any(filename.endswith(e) for e in (".mp3",".wav",".ogg",".flac",".aac",".m4a")):
        media_type = "audio/" + filename.rsplit(".",1)[-1]
    else:
        media_type = "application/octet-stream"
    return StreamingResponse(
        iter([result["data"]]),
        media_type=media_type,
        headers={"Content-Disposition": f'inline; filename="{result["filename"]}"'},
    )


@router.get("/files/{service}/info")
async def file_info(service: str, path: str,
                    instance_id: int | None = Query(None),
                    user: User = Depends(get_current_user_from_cookie), db: Session = Depends(get_db)):
    scoped, _ = _file_scope(user, service, instance_id, db)
    result = get_file_info(scoped, service, path)
    if result.get("error"):
        return JSONResponse({"ok": False, "error": result["error"]})
    type_map = {"file": "文件", "dir": "文件夹", "link": "符号链接", "unknown": "未知"}
    result["type_cn"] = type_map.get(result.get("type", ""), result.get("type", ""))
    result["ok"] = True
    return JSONResponse(result)
