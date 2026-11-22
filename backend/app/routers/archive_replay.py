"""审计归档回放路由：回读持久档案、触发回放、查看归档质量证据。

与通用资源路由分开，固定路径优先于 /api/{module} 动态路由注册。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException

from app.services import service_for

router = APIRouter(prefix="/api", tags=["审计归档回放"])


def _audit_service():
    service = service_for("audit_log")
    if service is None:
        raise HTTPException(status_code=404, detail="审计日志模块未注册")
    return service


def _quality_service():
    service = service_for("quality_report")
    if service is None:
        raise HTTPException(status_code=404, detail="质量报告模块未注册")
    return service


@router.get("/audit-log/archives")
def list_archives() -> dict[str, Any]:
    """列出磁盘上全部审计档案，含回放索引确认状态。"""
    service = _audit_service()
    return {
        "archives": service.list_archived_audits(),
        "replay_index": service.get_replay_index(),
        "evidence": service.get_archive_evidence(),
    }


@router.get("/audit-log/archives/{record_id}")
def read_archive(record_id: str) -> dict[str, Any]:
    """按记录编号回读单条已封存审计档案（未确认也可读，但会明确标注）。"""
    service = _audit_service()
    record = service.read_archived_audit(record_id)
    if record is None:
        raise HTTPException(
            status_code=404, detail=f"审计档案 {record_id} 不存在或已损坏"
        )
    confirmed_ids = {
        str(item.get("record_id", ""))
        for item in service.get_replay_index().get("archives", [])
    }
    record["confirmed"] = record_id in confirmed_ids
    return record


@router.post("/audit-log/replay")
def replay_archives() -> dict[str, Any]:
    """从持久档案与回放索引重新加载归档，并同步质量统计。"""
    return _audit_service().replay_archived_audits()


@router.get("/quality-report/archive-view")
def archive_quality_view() -> dict[str, Any]:
    """回读归档质量证据：事件清单、异常记录编号及确认状态。"""
    return _quality_service().get_archive_quality_view()
