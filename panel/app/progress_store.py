from __future__ import annotations

import threading
import uuid
from datetime import datetime
from typing import Optional

from .database import SessionLocal
from .models import ProgressTask


class TaskAlreadyRunning(RuntimeError):
    def __init__(self, task: dict):
        super().__init__("已有后台任务正在执行")
        self.task = task


_write_lock = threading.RLock()


def task_to_dict(task: ProgressTask | None) -> dict:
    if not task:
        return {}
    return {
        "task_id": task.id,
        "kind": task.kind,
        "target": task.target,
        "status": task.status,
        "step": task.step or "",
        "detail": task.detail or "",
        "error": task.error or "",
        "done": task.status == "success",
        "running": task.status == "running",
        "started_at": task.started_at.isoformat() if task.started_at else None,
        "updated_at": task.updated_at.isoformat() if task.updated_at else None,
        "finished_at": task.finished_at.isoformat() if task.finished_at else None,
    }


def get_task(task_id: str) -> dict:
    db = SessionLocal()
    try:
        return task_to_dict(db.query(ProgressTask).filter_by(id=task_id).first())
    finally:
        db.close()


def get_current_task(username: str, target: Optional[str] = None) -> dict:
    db = SessionLocal()
    try:
        query = db.query(ProgressTask).filter_by(username=username)
        if target:
            query = query.filter_by(target=target)
        running = query.filter_by(status="running").order_by(
            ProgressTask.started_at.desc()
        ).first()
        if running:
            return task_to_dict(running)
        latest = query.order_by(ProgressTask.started_at.desc()).first()
        return task_to_dict(latest)
    finally:
        db.close()


def start_task(username: str, kind: str, target: str, step: str) -> dict:
    with _write_lock:
        db = SessionLocal()
        try:
            active = db.query(ProgressTask).filter_by(
                username=username, status="running"
            ).order_by(ProgressTask.started_at.desc()).first()
            if active:
                raise TaskAlreadyRunning(task_to_dict(active))
            task = ProgressTask(
                id=uuid.uuid4().hex,
                username=username,
                kind=kind,
                target=target,
                status="running",
                step=step,
                detail="",
                error="",
            )
            db.add(task)
            db.commit()
            db.refresh(task)
            return task_to_dict(task)
        finally:
            db.close()


def update_task(task_id: str, step: str, detail: str = "", *,
                done: bool = False, error: str = "") -> dict:
    with _write_lock:
        db = SessionLocal()
        try:
            task = db.query(ProgressTask).filter_by(id=task_id).first()
            if not task:
                return {}
            task.step = step
            task.detail = detail or ""
            task.error = error or ""
            task.updated_at = datetime.now()
            if error:
                task.status = "failed"
                task.finished_at = datetime.now()
            elif done:
                task.status = "success"
                task.finished_at = datetime.now()
            db.commit()
            db.refresh(task)
            return task_to_dict(task)
        finally:
            db.close()


def interrupt_running_tasks() -> int:
    """应用重启后原线程已不存在，将遗留 running 任务标记为中断。"""
    with _write_lock:
        db = SessionLocal()
        try:
            tasks = db.query(ProgressTask).filter_by(status="running").all()
            now = datetime.now()
            for task in tasks:
                task.status = "interrupted"
                task.error = "面板进程已重启，原后台任务已中断"
                task.step = "任务已中断"
                task.updated_at = now
                task.finished_at = now
            db.commit()
            return len(tasks)
        finally:
            db.close()
