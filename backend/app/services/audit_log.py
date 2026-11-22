"""audit_log 领域服务：审计日志管理，收集所有跨模块操作的审计记录。

跨模块数据流：
    asset_catalog → audit_log: 资产发布/归档审计
    tier_policy → audit_log: 策略生效/停用审计
    cold_storage → audit_log: 冷存迁移审计
    compliance_check → audit_log: 合规扫描审计
    audit_log → quality_report: 审计异常纳入质量报告

归档与回放设计：
    1. 提交归档先把完整审计档案落盘（audit_<记录编号>.json），再登记持久化的
       「归档回放索引」（archive_replay_index.json）。档案与登记分两步，索引采用
       临时文件 + 原子替换写入，避免部分写入产生半截登记。
    2. 档案文件写完整但索引登记失败时，记录仍是未确认状态：档案可以回读，但不会
       被当成完整归档结果计入质量统计；重启或再次归档时通过回放补登记。
    3. 进程重启后从磁盘读回档案与回放索引，把已确认档案重放回内存，同步质量报告；
       同一条事故记录按记录编号去重，首次确认与重放都只贡献一次。
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any

from app.services.base import BaseModuleService
from app.store import store

DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "data", "audit")

INDEX_FILENAME = "archive_replay_index.json"
ARCHIVE_PREFIX = "audit_"
ARCHIVE_SUFFIX = ".json"


class AuditLogService(BaseModuleService):
    MODULE = "audit_log"
    ENTITY = "审计记录"
    OPERATION_TERM = "校验"
    LIST_FIELDS = ["记录编号", "操作主体", "操作类型", "操作对象", "操作时间", "影响范围", "审计摘要", "归档状态"]
    REQUIRED_FIELDS = ["记录编号", "操作主体", "操作类型"]
    STATUS_ORDER = ["已记录", "已校验", "已归档", "已过期"]
    ACTION_RULES = {"确认校验": "已校验", "提交归档": "已归档", "标记过期": "已过期"}
    NEGATIVE_ACTIONS = []

    def __init__(
        self,
        data_dir: str | None = None,
        quality_dir: str | None = None,
        quality_service: Any | None = None,
    ):
        super().__init__()
        self.data_dir = data_dir or DATA_DIR
        self._quality_dir = quality_dir
        # 隔离运行（如测试）时可注入独立质量服务；为空时回退到全局服务容器。
        self._quality_service_override = quality_service
        # 故障注入钩子：默认关闭；测试可替换为抛出 OSError，模拟索引登记部分写入失败。
        self.index_write_fault: Exception | None = None
        os.makedirs(self.data_dir, exist_ok=True)
        # 进程启动即回读持久档案：先把已确认归档恢复进内存，质量聚合等质量报告
        # 服务就绪后由启动钩子或显式回放补齐。
        self.replay_archived_audits()

    # ------------------------------------------------------------------ 归档

    def archive_logs(self, entry_id: int) -> tuple[dict[str, Any] | None, str]:
        """归档审计日志：先写持久档案，再登记回放索引，最后同步质量统计。

        档案写入与索引登记严格分两步：
        - 档案已落盘但登记失败：返回失败信息，内存记录保持未确认，档案仍可回读；
        - 索引已确认同一记录：跳过重复归档，保证同一条事故只贡献一次；
        - 档案存在但索引缺失（上次登记失败）：本次只补登记，不覆盖原始档案。
        """
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None, f"{self.ENTITY} {entry_id} 不存在"

        record_id = str(entry.get("记录编号", ""))
        index = self._load_replay_index()
        already_confirmed = any(
            str(item.get("record_id", "")) == record_id for item in index.get("archives", [])
        )
        if already_confirmed:
            # 已确认归档再次触发：幂等对齐内存状态，不重复写档案、不重复计异常。
            self._mark_entry_archived(entry)
            return entry, f"{self.ENTITY} {entry_id} 已在回放索引中确认，跳过重复归档"

        archive_path = self._archive_path(record_id)
        re_registering = os.path.exists(archive_path)
        if re_registering:
            # 上次档案写完整但登记未生效：以磁盘档案为准补登记，不重写证据。
            record = self._read_json(archive_path)
            if record is None:
                record = self._build_archive_record(entry)
                self._atomic_write_json(archive_path, record)
        else:
            # 第一步：完整档案先落盘（持久化、可回读）。
            record = self._build_archive_record(entry)
            self._atomic_write_json(archive_path, record)

        # 第二步：登记归档回放索引。此步失败不影响已写好的档案。
        try:
            self._register_archive_index(record_id, self._archive_filename(record_id), record)
        except OSError as exc:
            return (
                None,
                f"审计档案已写入但归档登记失败：{exc}；档案可回读，重启或再次归档可补登记",
            )

        # 登记生效后才允许把内存记录标记为已归档，并同步质量统计。
        self._mark_entry_archived(entry)
        self._sync_quality_with_archives()

        if re_registering:
            message = f"{self.ENTITY} {entry_id} 档案已存在，归档登记补登成功"
        else:
            message = f"{self.ENTITY} {entry_id} 已归档"
        return entry, message

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        if action == "提交归档":
            return self.archive_logs(entry_id)
        return super().run_action(entry_id, action)

    # ------------------------------------------------- 档案回读（只读磁盘）

    def read_archived_audit(self, record_id: str) -> dict[str, Any] | None:
        """按记录编号回读已封存的审计档案；文件不存在或损坏返回 None。

        注意：能读到文件只代表档案存在，是否已确认归档以回放索引为准，
        返回结果会显式带上 confirmed 标记。
        """
        record = self._read_json(self._archive_path(str(record_id)))
        if record is None:
            return None
        confirmed_ids = {
            str(item.get("record_id", ""))
            for item in self._load_replay_index().get("archives", [])
        }
        record["confirmed"] = str(record.get("record_id", record_id)) in confirmed_ids
        return record

    def list_archived_audits(self) -> list[dict[str, Any]]:
        """列出磁盘上全部审计档案，逐条标注回放索引是否已确认。"""
        reserved_files = {"audit_summary.json", INDEX_FILENAME}
        confirmed_ids = {
            str(item.get("record_id", ""))
            for item in self._load_replay_index().get("archives", [])
        }
        records: list[dict[str, Any]] = []
        for fname in sorted(os.listdir(self.data_dir)):
            if fname in reserved_files:
                continue
            if not (fname.startswith(ARCHIVE_PREFIX) and fname.endswith(ARCHIVE_SUFFIX)):
                continue
            record_id = fname[len(ARCHIVE_PREFIX):-len(ARCHIVE_SUFFIX)]
            if not record_id:
                continue
            record = self._read_json(os.path.join(self.data_dir, fname))
            if record is None:
                continue
            record_id = str(record.get("record_id", record_id))
            record["file"] = fname
            record["confirmed"] = record_id in confirmed_ids
            records.append(record)
        return records

    def get_replay_index(self) -> dict[str, Any]:
        """返回持久化的归档回放索引（不含档案时为空清单）。"""
        return self._load_replay_index()

    # ------------------------------------------------------------ 归档回放

    def replay_archived_audits(self) -> dict[str, Any]:
        """读回持久档案并按回放索引重放，恢复内存状态并同步质量统计。

        - 只有回放索引中已确认的档案才会重放为「已归档」；
        - 未登记的孤立档案保持未确认，不进内存归档态、不计异常；
        - 按记录编号去重，同一条事故无论首次确认还是重放只贡献一次；
        - 索引指向的档案缺失时登记为 missing，不冒充完整结果。
        """
        index = self._load_replay_index()
        confirmed_items = [
            item for item in index.get("archives", [])
            if str(item.get("record_id", ""))
        ]

        replayed: list[dict[str, Any]] = []
        missing_files: list[str] = []
        seen_ids: set[str] = set()
        touched = False
        for item in confirmed_items:
            record_id = str(item.get("record_id", ""))
            if record_id in seen_ids:
                continue
            record = self.read_archived_audit(record_id)
            if record is None:
                missing_files.append(record_id)
                continue
            seen_ids.add(record_id)
            # 索引是确认状态的权威来源，异常标记随登记一起持久化。
            record["abnormal"] = bool(item.get("abnormal", record.get("abnormal", False)))
            record["confirmed"] = True
            self._upsert_replayed_entry(record)
            replayed.append(record)
            item["replayed"] = int(item.get("replayed", 0)) + 1
            item["last_replayed_at"] = self._now()
            touched = True

        # 孤立档案：文件完整但从未登记成功，单独暴露，不参与重放。
        unconfirmed_files = [
            record for record in self.list_archived_audits()
            if not record.get("confirmed")
        ]

        if touched:
            self._write_replay_index(index)

        quality = self._quality_service()
        if quality is not None:
            quality.aggregate_audit_archives(replayed)
            quality.write_archive_quality_view(self.get_archive_evidence())

        return {
            "replayed_count": len(replayed),
            "replayed_record_ids": [str(r.get("record_id", "")) for r in replayed],
            "unconfirmed_count": len(unconfirmed_files),
            "unconfirmed_record_ids": [
                str(r.get("record_id", "")) for r in unconfirmed_files
            ],
            "missing_index_files": missing_files,
        }

    def get_archive_evidence(self) -> dict[str, Any]:
        """归档证据汇总：质量报告据此对上具体事件及其确认/异常状态。"""
        confirmed_ids = {
            str(item.get("record_id", "")): item
            for item in self._load_replay_index().get("archives", [])
        }
        events: dict[str, dict[str, Any]] = {}
        confirmed_archives: list[dict[str, Any]] = []
        unconfirmed_files: list[dict[str, Any]] = []
        on_disk_ids: set[str] = set()

        for record in self.list_archived_audits():
            record_id = str(record.get("record_id", ""))
            on_disk_ids.add(record_id)
            if record_id in confirmed_ids:
                item = confirmed_ids[record_id]
                abnormal = bool(item.get("abnormal", record.get("abnormal", False)))
                event = {
                    "record_id": record_id,
                    "confirmed": True,
                    "abnormal": abnormal,
                    "operation": record.get("operation", ""),
                    "subject": record.get("subject", ""),
                    "target": record.get("target", ""),
                    "archived_at": record.get("archived_at", ""),
                    "file": record.get("file", ""),
                }
                events[record_id] = event
                confirmed_archives.append(event)
            else:
                unconfirmed_files.append({
                    "record_id": record_id,
                    "confirmed": False,
                    "abnormal": bool(record.get("abnormal", False)),
                    "operation": record.get("operation", ""),
                    "file": record.get("file", ""),
                })

        missing_index_files = [
            record_id for record_id in confirmed_ids if record_id not in on_disk_ids
        ]
        abnormal_ids = sorted(
            record_id for record_id, event in events.items() if event["abnormal"]
        )
        return {
            "generated_at": self._now(),
            "confirmed_count": len(confirmed_archives),
            "unconfirmed_count": len(unconfirmed_files),
            "abnormal_count": len(abnormal_ids),
            "abnormal_record_ids": abnormal_ids,
            "events": events,
            "confirmed_archives": confirmed_archives,
            "unconfirmed_files": unconfirmed_files,
            "missing_index_files": missing_index_files,
        }

    # ------------------------------------------------------------ 审计汇总

    def get_audit_summary(self) -> dict[str, Any]:
        """获取审计日志汇总。"""
        rows = store.rows(self.MODULE)
        by_type: dict[str, int] = {}
        by_subject: dict[str, int] = {}
        for row in rows:
            t = str(row.get("操作类型", ""))
            s = str(row.get("操作主体", ""))
            by_type[t] = by_type.get(t, 0) + 1
            by_subject[s] = by_subject.get(s, 0) + 1

        summary = {
            "total": len(rows),
            "by_operation_type": by_type,
            "by_subject": by_subject,
            "generated_at": self._now(),
        }
        path = os.path.join(self.data_dir, "audit_summary.json")
        self._atomic_write_json(path, summary)
        return summary

    # ------------------------------------------------------------ 内部工具

    def _build_archive_record(self, entry: dict[str, Any]) -> dict[str, Any]:
        record_id = str(entry.get("记录编号", ""))
        return {
            "record_id": record_id,
            "subject": str(entry.get("操作主体", "")),
            "operation": str(entry.get("操作类型", "")),
            "target": str(entry.get("操作对象", "")),
            "operated_at": str(entry.get("操作时间", "")),
            "scope": str(entry.get("影响范围", "")),
            "summary": str(entry.get("审计摘要", "")),
            "abnormal": bool(entry.get("abnormal", False)),
            "confirmed": True,
            "archived_at": self._now(),
        }

    def _mark_entry_archived(self, entry: dict[str, Any]) -> None:
        entry["status"] = "已归档"
        entry["pending"] = False
        entry["abnormal"] = bool(entry.get("abnormal", False))
        entry["归档状态"] = "已归档"

    def _upsert_replayed_entry(self, record: dict[str, Any]) -> dict[str, Any]:
        """把重放档案写回内存；记录编号已存在则只对齐归档态，不重复新增。"""
        rows = store.rows(self.MODULE)
        record_id = str(record.get("record_id", ""))
        for row in rows:
            if str(row.get("记录编号", "")) == record_id:
                row["status"] = "已归档"
                row["pending"] = False
                row["abnormal"] = bool(record.get("abnormal", False))
                row["归档状态"] = "已归档"
                return row

        next_id = max((int(r.get("id", 0)) for r in rows), default=0) + 1
        entry = {
            "id": next_id,
            "status": "已归档",
            "pending": False,
            "abnormal": bool(record.get("abnormal", False)),
            "记录编号": record_id,
            "操作主体": str(record.get("subject", "")),
            "操作类型": str(record.get("operation", "")),
            "操作对象": str(record.get("target", "")),
            "操作时间": str(record.get("operated_at", "")),
            "影响范围": str(record.get("scope", "")),
            "审计摘要": str(record.get("summary", "")),
            "归档状态": "已归档",
        }
        rows.append(entry)
        return entry

    def _register_archive_index(
        self, record_id: str, filename: str, record: dict[str, Any]
    ) -> None:
        index = self._load_replay_index()
        archives = index.setdefault("archives", [])
        for item in archives:
            if str(item.get("record_id", "")) == record_id:
                item["file"] = filename
                item["abnormal"] = bool(record.get("abnormal", False))
                item["confirmed"] = True
                item["indexed_at"] = self._now()
                break
        else:
            archives.append({
                "record_id": record_id,
                "file": filename,
                "abnormal": bool(record.get("abnormal", False)),
                "confirmed": True,
                "archived_at": str(record.get("archived_at", "")),
                "indexed_at": self._now(),
                "replayed": 0,
            })
        index["generated_at"] = self._now()
        self._write_replay_index(index)

    def _sync_quality_with_archives(self) -> None:
        evidence = self.get_archive_evidence()
        quality = self._quality_service()
        if quality is None:
            return
        quality.aggregate_audit_archives(evidence["confirmed_archives"])
        quality.write_archive_quality_view(evidence)

    def _quality_service(self):
        if self._quality_service_override is not None:
            return self._quality_service_override
        # 服务容器初始化到 audit_log 时，service_for 可能尚未定义（部分初始化），
        # 这里通过模块属性延迟获取并对循环导入做容错。
        try:
            import app.services as services_module

            service_for = getattr(services_module, "service_for", None)
        except ImportError:
            service_for = None
        if service_for is not None:
            service = service_for("quality_report")
            if service is not None:
                return service
        if self._quality_dir is not None:
            # 独立运行（如隔离测试）时按需构造，不依赖全局服务容器。
            from app.services.quality_report import QualityReportService

            return QualityReportService(data_dir=self._quality_dir)
        return None

    def _archive_filename(self, record_id: str) -> str:
        return f"{ARCHIVE_PREFIX}{record_id}{ARCHIVE_SUFFIX}"

    def _archive_path(self, record_id: str) -> str:
        return os.path.join(self.data_dir, self._archive_filename(record_id))

    def _index_path(self) -> str:
        return os.path.join(self.data_dir, INDEX_FILENAME)

    def _load_replay_index(self) -> dict[str, Any]:
        data = self._read_json(self._index_path())
        if not isinstance(data, dict) or not isinstance(data.get("archives"), list):
            return {"version": 1, "generated_at": self._now(), "archives": []}
        data.setdefault("version", 1)
        return data

    def _write_replay_index(self, index: dict[str, Any]) -> None:
        # 故障注入点：仅作用于索引登记，档案文件不受影响，用于复现
        # 「文件写完整、登记因部分写入失败未生效」的现场。
        if self.index_write_fault is not None:
            raise self.index_write_fault
        index["generated_at"] = self._now()
        self._atomic_write_json(self._index_path(), index)

    def _read_json(self, path: str) -> dict[str, Any] | None:
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return None

    def _atomic_write_json(self, path: str, payload: Any) -> None:
        """临时文件 + 原子替换，杜绝部分写入产生半截档案或半截索引。"""
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
