"""quality_report 领域服务：质量报告管理，接收 compliance_check 和 audit_log 上游事件。

跨模块数据流：
    asset_catalog → quality_report: 资产发布时生成质量报告种子
    compliance_check → quality_report: 合规检查结果纳入质量报告
    audit_log → quality_report: 审计异常纳入质量报告

归档回放支撑：
    aggregate_audit_archives 消费 audit_log 的归档回放索引聚合持久档案异常，
    按记录编号去重并落到固定编号的一条聚合报告上；archive_quality_view.json
    持久化具体事件及其确认状态，重新进入报告仍能对上具体审计事件。
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any

from app.services.base import BaseModuleService
from app.store import store

DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "data", "quality")

# 审计归档聚合报告使用固定编号：同一聚合反复重放只更新这一条，不会因重放产生重复报告。
AUDIT_ARCHIVE_REPORT_CODE = "QUAL-AUDIT-ARCHIVE"
ARCHIVE_VIEW_FILENAME = "archive_quality_view.json"


class QualityReportService(BaseModuleService):
    MODULE = "quality_report"
    ENTITY = "质量报告"
    OPERATION_TERM = "校验"
    LIST_FIELDS = ["报告编号", "关联资产", "检核时间", "完整性", "准确性", "及时性", "异常数", "趋势方向"]
    REQUIRED_FIELDS = ["报告编号", "关联资产", "检核时间"]
    STATUS_ORDER = ["生成中", "已生成", "已确认", "已归档"]
    ACTION_RULES = {"确认生成": "已生成", "标记确认": "已确认", "提交归档": "已归档"}
    NEGATIVE_ACTIONS = []

    def __init__(self, data_dir: str | None = None):
        super().__init__()
        self.data_dir = data_dir or DATA_DIR
        os.makedirs(self.data_dir, exist_ok=True)

    def generate_report(self, entry_id: int) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None, f"{self.ENTITY} {entry_id} 不存在"

        asset_id = str(entry.get("关联资产", ""))

        # 汇总质量指标
        completeness = self._calc_completeness(asset_id)
        accuracy = self._calc_accuracy(asset_id)
        timeliness = "实时"
        abnormal_count = self._count_abnormal(asset_id)

        entry["完整性"] = completeness
        entry["准确性"] = accuracy
        entry["及时性"] = timeliness
        entry["异常数"] = str(abnormal_count)
        entry["趋势方向"] = "恶化" if abnormal_count > 0 else "稳定"
        entry["检核时间"] = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        entry["status"] = "已生成"
        entry["pending"] = False
        entry["abnormal"] = abnormal_count > 0

        # 写入质量报告文件
        self._write_quality_report(asset_id, entry)

        return entry, f"{self.ENTITY} {entry_id} 已生成，异常数={abnormal_count}"

    def get_quality_dashboard(self) -> dict[str, Any]:
        """生成质量仪表盘汇总所有资产质量。"""
        rows = store.rows(self.MODULE)
        total = len(rows)
        abnormal = sum(1 for r in rows if r.get("abnormal"))

        summary = {
            "total_reports": total,
            "abnormal_assets": abnormal,
            "health_score": f"{max(0, 100 - abnormal * 10)}%",
            "generated_at": self._now(),
        }
        path = os.path.join(self.data_dir, "quality_dashboard.json")
        self._atomic_write_json(path, summary)

        return summary

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        if action == "确认生成":
            return self.generate_report(entry_id)
        return super().run_action(entry_id, action)

    # ------------------------------------------- 审计归档回放 → 质量统计

    def aggregate_audit_archives(self, confirmed_archives: list[dict[str, Any]]) -> dict[str, Any]:
        """按归档回放索引聚合持久档案异常，幂等更新固定编号的审计聚合报告。

        confirmed_archives 只包含回放索引已确认的档案；同一条事故记录按
        record_id 去重，首次确认与重放都只贡献一次，重放不会堆叠重复报告。
        """
        unique: dict[str, dict[str, Any]] = {}
        for archive in confirmed_archives:
            record_id = str(archive.get("record_id", ""))
            if not record_id or record_id in unique:
                continue
            unique[record_id] = archive

        abnormal_ids = sorted(
            record_id for record_id, archive in unique.items()
            if archive.get("abnormal")
        )
        abnormal_count = len(abnormal_ids)
        total_count = len(unique)

        quality_rows = store.rows(self.MODULE)
        report = self._find_archive_aggregate_report(quality_rows)
        if report is None:
            next_id = max((int(r.get("id", 0)) for r in quality_rows), default=0) + 1
            report = {"id": next_id}
            quality_rows.append(report)

        report.update({
            "status": "已生成",
            "pending": False,
            "abnormal": abnormal_count > 0,
            "报告编号": AUDIT_ARCHIVE_REPORT_CODE,
            "关联资产": "审计归档",
            "检核时间": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            "完整性": "已回读" if total_count > 0 else "无归档",
            "准确性": f"审计异常:{abnormal_count}",
            "及时性": "归档回放",
            "异常数": str(abnormal_count),
            "趋势方向": "恶化" if abnormal_count > 0 else "稳定",
            "归档证据总数": str(total_count),
            "异常记录编号": "、".join(abnormal_ids),
        })
        return dict(report)

    def write_archive_quality_view(self, evidence: dict[str, Any]) -> dict[str, Any]:
        """把归档证据（事件级确认/异常状态）持久化，供重新进入报告时回读。"""
        view = {
            "generated_at": self._now(),
            "confirmed_count": evidence.get("confirmed_count", 0),
            "unconfirmed_count": evidence.get("unconfirmed_count", 0),
            "abnormal_count": evidence.get("abnormal_count", 0),
            "abnormal_record_ids": evidence.get("abnormal_record_ids", []),
            "events": evidence.get("events", {}),
            "confirmed_archives": evidence.get("confirmed_archives", []),
            "unconfirmed_files": evidence.get("unconfirmed_files", []),
            "missing_index_files": evidence.get("missing_index_files", []),
        }
        self._atomic_write_json(
            os.path.join(self.data_dir, ARCHIVE_VIEW_FILENAME), view
        )
        return view

    def get_archive_quality_view(self) -> dict[str, Any]:
        """回读持久化的归档质量证据；文件缺失时给出空结构而非报错。"""
        path = os.path.join(self.data_dir, ARCHIVE_VIEW_FILENAME)
        data = self._read_json(path)
        if not isinstance(data, dict):
            return {
                "generated_at": self._now(),
                "confirmed_count": 0,
                "unconfirmed_count": 0,
                "abnormal_count": 0,
                "abnormal_record_ids": [],
                "events": {},
                "confirmed_archives": [],
                "unconfirmed_files": [],
                "missing_index_files": [],
            }
        return data

    # ------------------------------------------------------------ 指标计算

    def _calc_completeness(self, asset_id: str) -> str:
        """计算数据完整性：检查关联资产的分区和补数状态。"""
        all_modules = store.module_names()
        total_related = 0
        complete = 0
        for mod in all_modules:
            for row in store.rows(mod):
                if asset_id in str(row.get("关联资产", "")) or asset_id in str(row.get("目标资产", "")):
                    total_related += 1
                    if not row.get("pending"):
                        complete += 1
        if total_related == 0:
            return "未评估"
        ratio = complete / total_related
        return "高" if ratio >= 0.8 else "中" if ratio >= 0.5 else "低"

    def _calc_accuracy(self, asset_id: str) -> str:
        """从合规检查结果计算准确性。"""
        for fname in os.listdir(self.data_dir):
            if fname.startswith("compliance_check_") and fname.endswith(".json"):
                path = os.path.join(self.data_dir, fname)
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                checked = int(data.get("checked_count", 0))
                violations = int(data.get("violations", 0))
                if checked > 0:
                    return f"合规率:{1 - violations / max(checked, 1):.1%}"
        return "未评估"

    def _count_abnormal(self, asset_id: str) -> int:
        count = 0
        for mod in store.module_names():
            for row in store.rows(mod):
                if (asset_id in str(row.get("关联资产", "")) or
                        asset_id in str(row.get("目标资产", ""))):
                    if row.get("abnormal"):
                        count += 1
        return count

    def _write_quality_report(self, asset_id: str, entry: dict[str, Any]) -> None:
        report_id = str(entry.get("报告编号", ""))
        filename = f"quality_report_{report_id}_{asset_id}.json"
        path = os.path.join(self.data_dir, filename)
        record = {
            "report_id": report_id,
            "asset_id": asset_id,
            "check_time": self._now(),
            "completeness": str(entry.get("完整性", "")),
            "accuracy": str(entry.get("准确性", "")),
            "timeliness": str(entry.get("及时性", "")),
            "abnormal_count": str(entry.get("异常数", "")),
            "trend": str(entry.get("趋势方向", "")),
        }
        self._atomic_write_json(path, record)

    def _find_archive_aggregate_report(
        self, quality_rows: list[dict[str, Any]]
    ) -> dict[str, Any] | None:
        for row in quality_rows:
            if str(row.get("报告编号", "")) == AUDIT_ARCHIVE_REPORT_CODE:
                return row
        return None

    def _read_json(self, path: str) -> dict[str, Any] | None:
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return None

    def _atomic_write_json(self, path: str, payload: Any) -> None:
        """临时文件 + 原子替换，杜绝部分写入产生半截质量产物。"""
        directory = os.path.dirname(path) or "."
        os.makedirs(directory, exist_ok=True)
        tmp_path = f"{path}.tmp.{os.getpid()}"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2, default=str)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
        dir_fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        except OSError:
            pass
        finally:
            os.close(dir_fd)

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()
