"""审计归档读取/回放集成测试。

覆盖场景（对应需求）：
  1. 归档先写档案再登记回放索引，索引原子持久化；
  2. 档案写完整但索引登记失败时，记录保持未确认，档案仍可回读但不计入质量统计；
  3. 进程重启后从磁盘回读档案与回放索引，已确认证据可回看，质量异常数一致；
  4. 同一事故记录首次确认与重放按记录编号去重，只贡献一次；
  5. 再次归档与已有档案冲突时幂等处理，不覆盖档案、不重复计数；
  6. 质量报告用回放索引聚合持久档案异常，重入可对上事件及确认状态；
  7. 未确认文件不能冒充完整结果。

测试使用独立临时目录，不污染 app/data 下的示例数据。
"""
from __future__ import annotations

import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(__file__))

from app.services.audit_log import (
    AuditLogService,
    INDEX_FILENAME,
)
from app.services.quality_report import (
    AUDIT_ARCHIVE_REPORT_CODE,
    ARCHIVE_VIEW_FILENAME,
    QualityReportService,
)
from app.store import store

RED = "\033[91m"
GREEN = "\033[92m"
RESET = "\033[0m"

passed = 0
failed = 0


def check(desc, condition, detail=""):
    global passed, failed
    if condition:
        print(f"  {GREEN}PASS{RESET} {desc}")
        passed += 1
    else:
        print(f"  {RED}FAIL{RESET} {desc} {detail}")
        failed += 1
    return bool(condition)


def make_services(audit_dir: str, quality_dir: str):
    """构造隔离的审计/质量服务对，质量目录与全局实例隔离。"""
    quality = QualityReportService(data_dir=quality_dir)
    audit = AuditLogService(
        data_dir=audit_dir, quality_dir=quality_dir, quality_service=quality
    )
    return audit, quality


def find_audit_entry(record_id: str):
    for row in store.rows("audit_log"):
        if str(row.get("记录编号", "")) == record_id:
            return row
    return None


def find_archive_report():
    for row in store.rows("quality_report"):
        if str(row.get("报告编号", "")) == AUDIT_ARCHIVE_REPORT_CODE:
            return row
    return None


def test_normal_archive_writes_file_and_index(audit_dir, quality_dir):
    print("\n=== 测试1: 正常归档 → 档案文件 + 回放索引 + 质量聚合 ===")
    audit, quality = make_services(audit_dir, quality_dir)

    # 种子数据 id=2（AUDI-0002）是 abnormal 的已校验记录
    entry = store.find("audit_log", 2)
    check("找到异常审计记录 AUDI-0002", entry is not None and entry.get("abnormal"))

    result, msg = audit.archive_logs(2)
    check("归档成功返回记录", result is not None, msg)
    check("内存状态变为已归档", result is not None and result["status"] == "已归档")

    archive_path = os.path.join(audit_dir, "audit_AUDI-0002.json")
    check("审计档案文件已落盘", os.path.exists(archive_path))
    with open(archive_path, "r", encoding="utf-8") as f:
        record = json.load(f)
    check("档案含记录编号", record.get("record_id") == "AUDI-0002")
    check("档案携带异常标记", record.get("abnormal") is True)

    index_path = os.path.join(audit_dir, INDEX_FILENAME)
    check("回放索引文件已持久化", os.path.exists(index_path))
    with open(index_path, "r", encoding="utf-8") as f:
        index = json.load(f)
    ids = [item.get("record_id") for item in index.get("archives", [])]
    check("回放索引登记 AUDI-0002", ids == ["AUDI-0002"], str(ids))

    report = find_archive_report()
    check("质量聚合报告已生成", report is not None)
    check("聚合报告异常数=1", report is not None and report.get("异常数") == "1",
          str(report and report.get("异常数")))
    check("聚合报告标注异常记录编号",
          report is not None and report.get("异常记录编号") == "AUDI-0002")

    view_path = os.path.join(quality_dir, ARCHIVE_VIEW_FILENAME)
    check("归档质量视图已持久化", os.path.exists(view_path))
    with open(view_path, "r", encoding="utf-8") as f:
        view = json.load(f)
    check("视图确认数=1", view.get("confirmed_count") == 1)
    check("视图异常数=1", view.get("abnormal_count") == 1)
    check("视图事件可对上记录编号", "AUDI-0002" in view.get("events", {}))
    check("视图事件已确认", view["events"]["AUDI-0002"].get("confirmed") is True)


def test_registration_failure_keeps_unconfirmed(audit_dir, quality_dir):
    print("\n=== 测试2: 档案写完但索引登记失败 → 未确认、不冒充完整结果 ===")
    audit, quality = make_services(audit_dir, quality_dir)

    # 故障注入：模拟索引登记的部分写入失败
    audit.index_write_fault = OSError("模拟索引磁盘写入失败")

    result, msg = audit.archive_logs(1)
    check("登记失败时返回失败结果", result is None, msg)
    check("失败信息提示可补登记", "归档登记失败" in msg, msg)

    archive_path = os.path.join(audit_dir, "audit_AUDI-0001.json")
    check("档案文件仍然完整落盘", os.path.exists(archive_path))
    with open(archive_path, "r", encoding="utf-8") as f:
        record = json.load(f)
    check("档案内容可读且完整", record.get("record_id") == "AUDI-0001")

    entry = store.find("audit_log", 1)
    check("内存记录未被标记为已归档", entry.get("status") != "已归档", entry.get("status"))

    index_path = os.path.join(audit_dir, INDEX_FILENAME)
    if os.path.exists(index_path):
        with open(index_path, "r", encoding="utf-8") as f:
            index = json.load(f)
        ids = [item.get("record_id") for item in index.get("archives", [])]
    else:
        ids = []
    check("回放索引不含未确认记录", "AUDI-0001" not in ids, str(ids))

    report = find_archive_report()
    abnormal_in_report = report.get("异常数") if report else None
    check("未确认档案不贡献异常数", abnormal_in_report == "1", str(abnormal_in_report))

    evidence = audit.get_archive_evidence()
    unconfirmed_ids = [item["record_id"] for item in evidence["unconfirmed_files"]]
    check("证据区把该档案标为未确认", "AUDI-0001" in unconfirmed_ids,
          str(unconfirmed_ids))
    check("已确认数仍为1", evidence["confirmed_count"] == 1)
    check("异常数仍只来自已确认档案", evidence["abnormal_count"] == 1)

    on_disk = audit.list_archived_audits()
    target = next((r for r in on_disk if r["record_id"] == "AUDI-0001"), None)
    check("回读列表明确标注 confirmed=False",
          target is not None and target.get("confirmed") is False)


def test_restart_replay_restores_state(audit_dir, quality_dir):
    print("\n=== 测试3: 重启后回读档案与索引 → 证据可回看、异常数一致 ===")
    # 模拟进程重启：内存恢复种子，磁盘档案保留
    store.reset()
    check("重启后内存无已归档 AUDI-0002",
          find_audit_entry("AUDI-0002") is None
          or find_audit_entry("AUDI-0002").get("status") != "已归档")

    # 新进程：构造服务时自动回放
    audit, quality = make_services(audit_dir, quality_dir)

    entry = find_audit_entry("AUDI-0002")
    check("重放把 AUDI-0002 恢复为已归档",
          entry is not None and entry.get("status") == "已归档",
          str(entry and entry.get("status")))
    check("重放恢复异常标记", entry is not None and entry.get("abnormal") is True)

    # AUDI-0001 档案存在但未登记，重放不能把它变成已归档
    entry1 = find_audit_entry("AUDI-0001")
    check("未确认档案重放后仍非已归档",
          entry1 is not None and entry1.get("status") != "已归档",
          str(entry1 and entry1.get("status")))

    replay = audit.replay_archived_audits()
    check("显式回放只统计已确认档案", replay["replayed_count"] == 1,
          str(replay))
    check("回放结果识别未确认孤立档案", replay["unconfirmed_count"] == 1,
          str(replay))
    check("未确认清单包含 AUDI-0001",
          "AUDI-0001" in replay["unconfirmed_record_ids"])

    report = find_archive_report()
    check("重启后质量聚合异常数仍=1",
          report is not None and report.get("异常数") == "1",
          str(report and report.get("异常数")))
    check("聚合报告仍只有一条（重放不堆叠）",
          sum(1 for r in store.rows("quality_report")
              if str(r.get("报告编号", "")) == AUDIT_ARCHIVE_REPORT_CODE) == 1)

    view = quality.get_archive_quality_view()
    check("持久视图重入可对上事件", "AUDI-0002" in view.get("events", {}))
    check("持久视图事件确认状态保留",
          view["events"]["AUDI-0002"].get("confirmed") is True)

    read_back = audit.read_archived_audit("AUDI-0002")
    check("已确认证据刷新后仍能回看",
          read_back is not None and read_back.get("record_id") == "AUDI-0002")
    read_back1 = audit.read_archived_audit("AUDI-0001")
    check("未确认档案也能回读但显式标注",
          read_back1 is not None and read_back1.get("confirmed") is False)


def test_reregister_after_failure_then_idempotent(audit_dir, quality_dir):
    print("\n=== 测试4: 补登记 + 再次归档冲突 → 幂等、只贡献一次 ===")
    audit, quality = make_services(audit_dir, quality_dir)
    audit.index_write_fault = None  # 故障解除

    # 针对 AUDI-0001 再次归档：档案已在磁盘，应只补登记、不覆盖
    before = json.load(open(os.path.join(audit_dir, "audit_AUDI-0001.json"),
                            encoding="utf-8"))
    result, msg = audit.archive_logs(1)
    check("补登记成功", result is not None, msg)
    after = json.load(open(os.path.join(audit_dir, "audit_AUDI-0001.json"),
                           encoding="utf-8"))
    check("补登记不覆盖原始档案证据",
          before.get("archived_at") == after.get("archived_at"),
          f"{before.get('archived_at')} != {after.get('archived_at')}")
    check("补登记提示为补登", "补登" in msg, msg)

    index = json.load(open(os.path.join(audit_dir, INDEX_FILENAME),
                           encoding="utf-8"))
    ids = sorted(item.get("record_id") for item in index["archives"])
    check("索引包含两条已确认记录", ids == ["AUDI-0001", "AUDI-0002"], str(ids))

    evidence = audit.get_archive_evidence()
    check("未确认数清零", evidence["unconfirmed_count"] == 0)
    check("确认数=2", evidence["confirmed_count"] == 2)

    # 对同一记录再次归档：幂等跳过
    result2, msg2 = audit.archive_logs(1)
    check("重复归档被幂等接受", result2 is not None and "跳过重复归档" in msg2, msg2)
    index2 = json.load(open(os.path.join(audit_dir, INDEX_FILENAME),
                            encoding="utf-8"))
    check("重复归档不增加索引条目", len(index2["archives"]) == 2)

    # 多次回放，同一条事故只贡献一次
    audit.replay_archived_audits()
    audit.replay_archived_audits()
    report = find_archive_report()
    check("多次回放后异常数仍只=1（AUDI-0001 非异常）",
          report is not None and report.get("异常数") == "1",
          str(report and report.get("异常数")))
    check("聚合报告始终唯一",
          sum(1 for r in store.rows("quality_report")
              if str(r.get("报告编号", "")) == AUDIT_ARCHIVE_REPORT_CODE) == 1)
    check("内存中 AUDI-0001 只有一条",
          sum(1 for r in store.rows("audit_log")
              if str(r.get("记录编号", "")) == "AUDI-0001") == 1)


def test_corrupt_and_missing_files(audit_dir, quality_dir):
    print("\n=== 测试5: 损坏档案与索引悬空登记的容错 ===")
    # 写一个损坏的孤立档案
    corrupt_path = os.path.join(audit_dir, "audit_AUDI-BAD.json")
    with open(corrupt_path, "w", encoding="utf-8") as f:
        f.write("{这不是合法JSON")
    audit, quality = make_services(audit_dir, quality_dir)
    listed = audit.list_archived_audits()
    check("损坏档案不会出现在回读列表",
          all(r.get("record_id") != "AUDI-BAD" for r in listed))

    # 构造索引指向缺失文件
    index_path = os.path.join(audit_dir, INDEX_FILENAME)
    index = json.load(open(index_path, encoding="utf-8"))
    index["archives"].append({
        "record_id": "AUDI-GHOST",
        "file": "audit_AUDI-GHOST.json",
        "abnormal": True,
        "confirmed": True,
        "archived_at": "2026-11-22T00:00:00+00:00",
        "indexed_at": "2026-11-22T00:00:00+00:00",
        "replayed": 0,
    })
    with open(index_path, "w", encoding="utf-8") as f:
        json.dump(index, f, ensure_ascii=False)

    replay = audit.replay_archived_audits()
    check("悬空登记被识别为 missing",
          "AUDI-GHOST" in replay["missing_index_files"], str(replay))
    check("悬空登记不冒充证据（回放数仍为2）",
          replay["replayed_count"] == 2, str(replay))
    evidence = audit.get_archive_evidence()
    check("证据区暴露悬空登记",
          "AUDI-GHOST" in evidence["missing_index_files"])
    check("异常数不被悬空登记抬高", evidence["abnormal_count"] == 1)


def test_report_before_after_restart_equivalence(audit_dir, quality_dir):
    print("\n=== 测试6: 切断登记→重启→再归档，前后报告凭据与异常数对比 ===")
    store.reset()
    # 全新隔离环境，重新走一遍完整故事线
    audit, quality = make_services(audit_dir, quality_dir)

    # 故障窗口：AUDI-0002 归档时索引登记失败
    audit.index_write_fault = OSError("模拟登记部分写入")
    result, _ = audit.archive_logs(2)
    check("故障窗口归档返回未确认", result is None)
    before = quality.get_archive_quality_view()
    before_abnormal = before["abnormal_count"]
    before_confirmed = before["confirmed_count"]
    check("故障窗口报告确认数=0", before_confirmed == 0, str(before_confirmed))
    check("故障窗口报告异常数=0（未确认不冒充）", before_abnormal == 0,
          str(before_abnormal))
    check("故障窗口档案仍可回读",
          audit.read_archived_audit("AUDI-0002") is not None)

    # 重启
    store.reset()
    audit2, quality2 = make_services(audit_dir, quality_dir)
    mid = quality2.get_archive_quality_view()
    check("重启后未确认档案仍不计异常", mid["abnormal_count"] == 0)
    check("重启后证据区可见未确认文件",
          any(item["record_id"] == "AUDI-0002"
              for item in mid["unconfirmed_files"]))

    # 再次归档：补登记成功
    audit2.index_write_fault = None
    result2, msg2 = audit2.archive_logs(2)
    check("再次归档补登记成功", result2 is not None, msg2)
    after = quality2.get_archive_quality_view()
    check("补登记后确认数=1", after["confirmed_count"] == 1)
    check("补登记后异常数=1（前后差异来自确认而非重复计数）",
          after["abnormal_count"] == 1)
    check("报告可对上具体事件",
          after["events"].get("AUDI-0002", {}).get("confirmed") is True)

    # 再重启，数字必须与补登记后完全一致
    store.reset()
    audit3, quality3 = make_services(audit_dir, quality_dir)
    final = quality3.get_archive_quality_view()
    check("二次重启确认数保持1", final["confirmed_count"] == 1)
    check("二次重启异常数保持1", final["abnormal_count"] == 1)
    check("二次重启后证据仍可回看",
          audit3.read_archived_audit("AUDI-0002") is not None)


def main():
    print("=" * 60)
    print("审计归档读取与回放集成测试")
    print("=" * 60)

    with tempfile.TemporaryDirectory(prefix="audit-replay-") as root:
        run_dir = 1

        def dirs():
            nonlocal run_dir
            base = os.path.join(root, f"case-{run_dir}")
            run_dir += 1
            audit_dir = os.path.join(base, "audit")
            quality_dir = os.path.join(base, "quality")
            os.makedirs(audit_dir, exist_ok=True)
            os.makedirs(quality_dir, exist_ok=True)
            store.reset()
            return audit_dir, quality_dir

        d_audit, d_quality = dirs()
        test_normal_archive_writes_file_and_index(d_audit, d_quality)
        test_registration_failure_keeps_unconfirmed(d_audit, d_quality)
        test_restart_replay_restores_state(d_audit, d_quality)
        test_reregister_after_failure_then_idempotent(d_audit, d_quality)
        test_corrupt_and_missing_files(d_audit, d_quality)

        d_audit2, d_quality2 = dirs()
        test_report_before_after_restart_equivalence(d_audit2, d_quality2)

    print(f"\n{'=' * 60}")
    print(f"结果: {GREEN}{passed} 通过{RESET} / {RED}{failed} 失败{RESET} / "
          f"{passed + failed} 总计")
    print("=" * 60)
    if failed > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
