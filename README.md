# 数据资产与生命周期治理平台

面向数据资产编目、冷热分层、生命周期管理、血缘追踪与合规治理的统一数据治理平台。

这是一个前后端分离的管理平台：前端 Vue 3 + Vite + TypeScript，后端 FastAPI（Python）。
两边各自独立启动，前端 dev server 已关掉自动打开页面，启动后按终端打印的地址手工打开。

## 目录结构

```text
.
├── frontend/                 Vue 3 + Vite + TypeScript 前端
│   ├── src/views/WorkbenchView.vue|tsx  全部模块共享工作台
│   ├── src/data/modules.ts              18个业务模块配置
│   ├── src/api/              统一请求封装
│   └── vite.config.ts        dev server 配置（open: false）
├── backend/                  FastAPI（Python） 后端
│   ├── app/routers/resource.py  通用查询/导出/登记/动作接口
│   ├── app/services/<key>.py    每个模块独立领域逻辑
│   ├── app/services/base.py     共享服务骨架
│   └── app/store.py          内存数据仓库与示例数据
├── .gitignore
└── docker-compose.yml
```

## 启动

### 后端

```bash
cd backend
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
./run.sh
```

健康检查：`curl http://127.0.0.1:8000/api/health`

### 前端

```bash
cd frontend
npm ci
npm run dev
```

前端默认监听 `http://127.0.0.1:5173/`，dev server 不会自动打开浏览器，
需要自己访问。`/api` 由 vite 代理到后端 `http://127.0.0.1:8000`。

## 业务模块

| 模块 | 目录 | 业务对象 | 主要字段 |
| --- | --- | --- | --- |
| 资产编目 | `asset_catalog` | 数据资产 | 资产编号、资产名称、所属域 |
| 分层策略 | `tier_policy` | 分层策略 | 策略编号、目标资产、当前层级 |
| 血缘图谱 | `lineage_graph` | 血缘记录 | 记录编号、上游资产、下游资产 |
| 保留规则 | `retention_rule` | 保留规则 | 规则编号、目标资产、保留天数 |
| 合规检查 | `compliance_check` | 合规规则 | 规则编号、检查维度、合规标准 |
| 冷存储管理 | `cold_storage` | 冷存任务 | 任务编号、源资产、目标存储 |
| 预热回迁 | `hot_prefetch` | 回迁任务 | 任务编号、冷存位置、目标分区 |
| 版本快照 | `version_snapshot` | 数据快照 | 快照编号、关联资产、快照时间 |
| 标签体系 | `label_tag` | 数据标签 | 标签编号、标签名称、标签类型 |
| 审计日志 | `audit_log` | 审计记录 | 记录编号、操作主体、操作类型 |
| 访问网关 | `access_gate` | 访问策略 | 策略编号、目标资产、授权主体 |
| 成本追踪 | `cost_tracker` | 成本记录 | 记录编号、关联资产、存储成本 |
| Schema注册 | `schema_registry` | Schema版本 | 版本编号、关联资产、字段清单 |
| 分区规划 | `partition_plan` | 分区方案 | 方案编号、关联资产、分区键 |
| 补数任务 | `backfill_job` | 补数作业 | 作业编号、目标资产、补数区间 |
| 质量报告 | `quality_report` | 质量报告 | 报告编号、关联资产、检核时间 |
| 回收清理 | `reclaim_job` | 回收任务 | 任务编号、目标资产、回收范围 |
| 变更通知 | `change_notice` | 变更通知 | 通知编号、关联资产、变更类型 |

## 约定

- 全部模块共享`frontend/src/views/WorkbenchView.vue|tsx`和一套资源路由。
- 每个模块仍保留独立`backend/app/services/<模块>.py`；纯前端保留
  `frontend/src/domain/<模块>.ts`，用于承载真实业务差异并维持36题位容量。
- 列表接口统一返回 `{ items, total, page, size }`，动作接口统一返回 `{ ok, message }`。
- 状态流转只允许在 `app/services` 里改，路由层不做业务判断。

## 审计归档回放

审计归档把「档案文件」和「归档登记」拆成两步，保证进程重启后质量统计仍能对上：

- 提交归档先把完整档案原子落盘到 `backend/app/data/audit/audit_<记录编号>.json`，
  再登记持久化的**归档回放索引** `archive_replay_index.json`（临时文件 + `os.replace`
  原子替换，杜绝部分写入产生半截登记）。
- 档案已写完整但登记失败时，记录保持未确认：档案可回读，但不计入质量统计；
  重启或再次归档时只补登记、不覆盖原始证据。
- 进程启动（`app/main.py` 的 lifespan）自动调用 `replay_archived_audits`，
  按回放索引把已确认档案重放回内存；同一条事故按记录编号去重，首次确认与
  重放都只贡献一次。
- 质量报告服务用回放索引聚合持久档案异常，固定编号 `QUAL-AUDIT-ARCHIVE` 的
  聚合报告幂等更新，并把事件级确认/异常状态持久化到
  `backend/app/data/quality/archive_quality_view.json`。
- 未确认文件在所有接口与前端面板上都明确标注，不能冒充完整归档结果。

回放相关接口（固定路径，先于通用 `/api/{module}` 注册）：

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/audit-log/archives` | 列出全部审计档案、回放索引与确认证据 |
| GET | `/api/audit-log/archives/{record_id}` | 回读单条封存档案（含 confirmed 标记） |
| POST | `/api/audit-log/replay` | 触发持久档案回放并同步质量统计 |
| GET | `/api/quality-report/archive-view` | 回读归档质量证据（事件与确认状态） |

后端测试：`test_cross_module.py`（既有跨模块链）、`test_archive_replay.py`
（归档/登记失败/重启/补登记/幂等/容错）、`test_archive_routes.py`（HTTP 路由，
基于内置 ASGI 调用器，无需额外依赖）。

领域：数据工程
子方向：数仓与数据湖
