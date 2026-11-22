"""轻量 ASGI 调用器：不依赖 httpx，直接按 ASGI 协议请求 FastAPI 应用。"""
from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.main import app  # noqa: E402
from app.services import service_for  # noqa: E402
from app.store import store  # noqa: E402


async def call(method: str, path: str) -> tuple[int, bytes]:
    if "?" in path:
        raw_path, query = path.split("?", 1)
    else:
        raw_path, query = path, ""
    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": raw_path,
        "raw_path": raw_path.encode(),
        "query_string": query.encode(),
        "headers": [(b"host", b"testserver"), (b"content-type", "application/json")],
        "client": ("testclient", 50000),
        "server": ("testserver", 80),
    }
    chunks: list[bytes] = []
    status_holder: dict[str, int] = {}

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        if message["type"] == "http.response.start":
            status_holder["code"] = message["status"]
        elif message["type"] == "http.response.body":
            chunks.append(message.get("body", b""))

    await app(scope, receive, send)
    return status_holder["code"], b"".join(chunks)


def main() -> None:
    failures = 0

    def expect(desc: str, ok: bool, detail: str = "") -> None:
        nonlocal failures
        mark = "PASS" if ok else "FAIL"
        print(f"  {mark} {desc} {detail}")
        if not ok:
            failures += 1

    # 1. 走真实归档动作（服务容器内的单例）
    audit = service_for("audit_log")
    entry, msg = audit.archive_logs(2)
    expect("服务层归档 AUDI-0002 成功", entry is not None, msg)

    # 2. 固定路由不被 /api/{module} 抢占
    code, body = asyncio.run(call("GET", "/api/audit-log/archives"))
    expect("GET /api/audit-log/archives 命中固定路由", code == 200, f"status={code}")
    payload = json.loads(body)
    expect("档案列表含 AUDI-0002",
           any(r.get("record_id") == "AUDI-0002" for r in payload["archives"]))
    expect("证据区确认数>=1", payload["evidence"]["confirmed_count"] >= 1)

    code, body = asyncio.run(call("GET", "/api/audit-log/archives/AUDI-0002"))
    expect("回读单条档案 200", code == 200, f"status={code}")
    record = json.loads(body)
    expect("回读档案带 confirmed=true", record.get("confirmed") is True)

    code, _ = asyncio.run(call("GET", "/api/audit-log/archives/NO-SUCH-ID"))
    expect("回读不存在档案 404", code == 404, f"status={code}")

    code, body = asyncio.run(call("POST", "/api/audit-log/replay"))
    expect("POST 回放接口 200", code == 200, f"status={code}")
    replay = json.loads(body)
    expect("回放结果结构完整",
           {"replayed_count", "unconfirmed_count", "missing_index_files"} <= set(replay))

    code, body = asyncio.run(call("GET", "/api/quality-report/archive-view"))
    expect("质量证据视图接口 200", code == 200, f"status={code}")
    view = json.loads(body)
    expect("视图可对上事件与确认状态",
           view["events"].get("AUDI-0002", {}).get("confirmed") is True)
    expect("视图异常数>=1", view["abnormal_count"] >= 1)

    # 3. 动态资源路由仍然可用
    code, body = asyncio.run(call("GET", "/api/audit_log?page=1&size=5"))
    expect("通用资源路由仍可访问", code == 200, f"status={code}")
    page = json.loads(body)
    expect("资源分页结构保留", {"items", "total", "page", "size"} <= set(page))

    code, body = asyncio.run(call("GET", "/api/health"))
    expect("健康检查正常", code == 200 and json.loads(body)["ok"] is True)

    total_checks = 13
    print(f"\nHTTP 路由验证：{total_checks - failures} 通过 / {failures} 失败")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
