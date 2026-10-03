# 职业培训分镜应用 Training Studio

面向职业培训课程的**分镜（storyboard）工作台**：在 Web 端维护训练目标、章节、
讲师台词、操作演示、练习提示与考核点；API 连接持久数据库（SQLite）；后台工作器
异步生成**可预览的不可变课件包**。

仅依赖 Python 3 标准库（`http.server` + `sqlite3` + `threading`），无需安装任何第三方包。

## 运行

```bash
cd training-studio
python3 -m app.server --db studio.db --port 8000
# 打开 http://127.0.0.1:8000
```

启动后内置后台工作器线程（轮询 `jobs` 表）。首次打开页面会自动创建一个演示团队
和四个账号（管理员 / 老师甲 / 老师乙 / 观察者），可在右上角切换身份。

## 验收测试

```bash
cd training-studio
python3 -m unittest discover -s tests -v
```

13 个用例覆盖需求中列出的全部验收场景（见文末对照表）。

## 目录结构

```
app/db.py        持久层：SQLite 单连接 + RLock，写事务 BEGIN IMMEDIATE（HTTP 线程与 worker 共享）
app/services.py  领域服务：版本条件 / 字段级撤销 / 前置重验证 / 素材一致性 / 覆盖报告 / 幂等导出+额度 / 快照
app/worker.py    后台工作器：轮询 jobs，生成课件包，持久化回执
app/api.py       REST API + 微型路由 + 认证鉴权（X-User-Id）
app/server.py    服务入口：静态 Web 端 + API + worker 线程
static/          纯原生 JS 单页工作台
tests/           验收测试
```

## 数据模型（16 张表）

`teams / users / quotas`、`courses / objectives / chapters / segments /
assessment_points`、`assets / asset_versions / segment_issues`、
`packages`（不可变快照）、`jobs`（幂等键 + 回执）、`classes`（pinned/follow）、
`revisions + entity_heads`（逐字段修订与每实体版本号）。

关键约束：

- `segments.kind ∈ lecture(讲师台词) | demo(操作演示) | exercise(练习提示)`；
  demo 通过 `asset_version_id` **绑定同一个素材版本**，该版本同时承载
  画面 `video_uri` / 字幕 `subtitle_uri` / 说明 `notes_uri`。
- `chapters.requires` 是前置章节 id 的 JSON 数组。
- `asset_versions UNIQUE(asset_id, version)`；`jobs.idempotency_key UNIQUE`。
- `packages.snapshot` 是导出时刻整门课的 JSON 快照，`sha256` 校验内容，一经生成不可修改。

## 核心领域规则

### 1. 结构写操作后“重新验证知识前置关系”，而非只更新总分钟数

章节重排（`/chapters/reorder`）、删除章节、删除演示片段、绑定/替换素材、改时间码后，
都会调用 `validate_course()` 对**整门课**重算问题列表并随写接口返回，不只是刷新派生数字。

问题码：

| code | 含义 |
|---|---|
| `prerequisite_missing` | 章节引用的前置章节已删除（悬空） |
| `prerequisite_order` | 章节排在其前置章节之前 |
| `assessment_orphan` | 考核点引用的教学片段已删除 |
| `objective_missing` | 考核点引用的目标不存在 |
| `demo_asset_missing` / `video_missing` / `subtitle_missing` / `notes_missing` | 操作演示未绑素材或缺画面/字幕/说明 |
| `timecode_out_of_range` | 时间码超出所绑素材版本时长 |
| `segment_pending_repair` | 片段处于待修复状态 |
| `audio_missing` | 素材音轨缺失（**告警**，不阻断导出，随包留存） |

### 2. 画面/字幕/说明必须同一素材版本；替换视频后超界时间码进入待修复

- 一个素材有多个不可变 `asset_versions`；demo 片段只能整体绑定其中一个版本，
  因此画面、字幕、说明天然同源，不允许跨版本拼装。
- 绑定新版本或修改时间码后 `refresh_repair_state()` 重算：任一时间码
  `at_ms > duration_ms` 即写入 `timecode_out_of_range` issue 并把片段置为
  `pending_repair`（旧 issue 标记 resolved）；时间码改回范围内自动恢复 `ok`。

### 3. 每个考核点引用“支撑它的教学片段”

`assessment_points(objective_id → segment_id)`。覆盖报告 `/coverage` 列出
**当前版本未覆盖的训练目标**；删除支撑演示后，考核点变悬空（`assessment_orphan`）、
对应目标立即出现在 `uncovered` / `uncovered_objective_ids` 中。训练目标改版
（标题更新）后报告按当前目标内容重新呈现。

### 4. 已开班冻结：固定课件版本复用 vs 模块跟随更新

- `classes.mode = pinned`：开班时锁定一个不可变 `packages.id`（`pinned_package_id`）。
  此后模板升级、章节改名、素材替换、再次导出 **都不会影响**学员正在使用的内容——
  `/classes/{id}/package` 永远返回被冻结的那份快照。
- `mode = follow`：班级不锁定，解析时返回课程最新课件包；模板升级 + 重新导出后自动浮动。
  `/impact` 给出每个已开班实际生效的版本与 `frozen / follow` 说明。

### 5. 多人编辑：版本条件（乐观锁）

每个实体在 `entity_heads` 中有单调递增的 `head_rev`。写操作必须携带
`expected_rev`（PUT/POST 在 body，DELETE 在 query）。不一致返回
`409 conflict`「版本冲突…请刷新后重试」。每次 create/update/delete/undo 都在
`revisions` 留痕（含 before/after JSON、操作人、归属课程）。

### 6. 字段级撤销，绝不回退同事的后续修订

`POST /revisions/{id}/undo` 采用**补偿式字段撤销**：

- 只回退该修订引入、且当前值仍等于该修订落地值的字段；
- 已被同事后续修订改动的字段进入 `skipped`，保持现值；全字段被覆盖时返回
  `status=skipped` 且**不产生新版本号**（版本号只在真实状态变化时前移）；
- 撤销删除会把整行按 before 重新插回（`delete_reverted`）；撤销创建在已有后续修订时拒绝。

### 7. 导出：幂等作业 + 额度一致 + 回执可重取

- `POST /courses/{id}/export` 带 `idempotency_key`。同一键的重复/并发提交只生成
  **一个**作业、只扣减**一次**额度（`UNIQUE` 键 + `BEGIN IMMEDIATE` 事务保证）；
  响应 `deduplicated` 标识是否为去重命中，且在额度已用尽后重放旧键仍成功返回原作业。
- 作业经后台工作器处理：`queued → running → succeeded/failed`。成功时生成课件包并把
  **回执（`rcpt-<sha256>` 前缀）持久化到 jobs.receipt**；客户端即使在导出成功后
  回执“丢失”，也可随时 `GET /jobs/{id}/receipt` 重新取回。
- `audio_missing` 等问题不阻断导出，而是写入包的 `issues` 与作业 `result.issues`。

### 8. 老师离开团队

`POST /users/{id}/leave` 将 `users.active` 置 0：

- 之后所有写操作返回 `403 member_inactive`（仅本人或管理员可办理）；
- 读操作（课程树、校验、覆盖、**历史**）仍可按权限查看，历史中保留其署名。

## 权限

- 认证：`X-User-Id` 请求头（演示方案）。
- 角色：`admin / editor / viewer`；写操作要求 active 且非 viewer。
- 离职成员与 viewer：只读；建团队 / 建成员为引导用匿名端点。

## 主要 API

```
POST   /teams                     POST /teams/{tid}/users      GET /teams/{tid}/users
GET    /teams/{tid}/quota         GET  /teams/{tid}/courses    GET /teams/{tid}/assets
POST   /teams/{tid}/assets        POST /assets/{aid}/versions  POST /users/{uid}/leave
POST   /courses                   GET  /courses/{cid}           PUT  /courses/{cid}
POST   /courses/{cid}/objectives  PUT  /objectives/{oid}
POST   /courses/{cid}/chapters    PUT  /chapters/{chid}        DELETE /chapters/{chid}?expected_rev=
POST   /courses/{cid}/chapters/reorder
POST   /chapters/{chid}/segments  PUT  /segments/{sid}         DELETE /segments/{sid}?expected_rev=
POST   /segments/{sid}/bind_asset POST /segments/{sid}/timecodes
POST   /courses/{cid}/assessments DELETE /assessments/{aid}
GET    /courses/{cid}/validation  GET  /courses/{cid}/coverage
GET    /courses/{cid}/history     GET  /courses/{cid}/packages
GET    /courses/{cid}/impact      POST /courses/{cid}/export
GET    /jobs/{jid}                GET  /jobs/{jid}/receipt
POST   /courses/{cid}/classes     GET  /classes/{clid}/package
POST   /revisions/{rid}/undo
```

## 验收场景对照表

| 需求验收点 | 用例 |
|---|---|
| 删除前置章节 / 重排后重新验证前置关系 | `TestPrerequisiteRevalidation` |
| 考核点引用支撑片段；删演示后列未覆盖目标 | `TestAssessmentCoverage` |
| 音轨缺失被标记 | `TestAssetVersionConsistency.test_missing_audio_track_flagged_and_packaged` |
| 训练目标改版后覆盖报告 | `TestObjectiveRevisionCoverage` |
| 导出成功但回执丢失可重取；重复导出去重、额度一致 | `TestExportIdempotencyQuotaReceipt` |
| 老师离开团队；工作区仍可按权限查看历史 | `TestTeamMembership` |
| 多人版本条件冲突；撤销不回退同事修订 | `TestConcurrencyAndUndo` |
| 已开班 pinned 冻结 vs follow 跟随 | `TestFreezeRules` |
| 替换视频后超界时间码进入待修复 | `TestAssetVersionConsistency.test_replace_video_out_of_range_timecode_pending_repair` |
