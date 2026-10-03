# 职业培训分镜工作台

面向职业培训的**课程分镜（Storyboard）Web 应用**：在 Web 端维护课程目标、章节、
讲师台词、练习提示与操作演示素材；API 连接持久化 SQLite 数据库；后台工作器异步
生成可预览的课件包；围绕“**前置关系重验证、素材版本一致性、开班冻结、多人协作
冲突、撤销安全、导出幂等/配额/回执**”等教学内容工程问题给出完整实现。

仅依赖 Python 3 标准库（后端 + 后台工作器）和原生 HTML/CSS/JS（前端），无需 npm / pip。

## 一分钟启动

```bash
python3 run_server.py            # 默认 http://localhost:8000
# 可选：--db data/x.db --artifacts artifacts --port 8000 --no-worker
```

首次启动自动建表并写入演示课程「电工基础实训」（含 3 章、5 片段、3 目标、
1 个演示素材的画面/字幕/说明绑定）。前端右上角可切换用户体验不同角色：

| 用户 | 角色 | 用途 |
|---|---|---|
| Anna 管理员 (1) | admin | 成员管理、回执对账 |
| Beth 编辑 (2) / Carl 编辑 (3) / Tom 讲师 (5) | editor | 课程编辑、导出、开班 |
| Vera 访客 (4) | viewer | 只读，可看历史 |

所有 API 通过 `X-User-Id` 头鉴权（演示用，生产应替换为会话/JWT）。

## 目录结构

```
run_server.py            启动入口（建库+种子+工作器+HTTP）
server/
  db.py                  SQLite 连接/Schema/事务助手
  domain.py              领域服务（核心业务规则，见下）
  app.py                 HTTP API：路由、鉴权、权限矩阵、乐观并发(409)、审计
  worker.py              后台工作器：轮询 jobs，产物与回执同事务提交
  seed.py                演示数据
web/                     单页前端（index.html / app.js / styles.css）
tests/                   30 个测试（unittest，标准库）
artifacts/               生成的课件包 JSON
```

运行测试：

```bash
python3 -m unittest discover -s tests -v
```

## 核心业务规则与对应代码

| 需求关注点 | 实现要点 | 位置 |
|---|---|---|
| **删除前置章节 / 重排后重验证知识前置关系** | `validate_course()` 用三色 DFS 检测环、检测悬挂边、检测“章节排在前置之前”；重排/删章接口返回完整校验报告，**绝非只更新总分钟数**（分钟数仅作为附带字段） | `domain.validate_course`，`app.reorder_chapters/delete_chapter` |
| **考核点引用支撑教学片段** | `objectives ← objective_coverage → segments`；覆盖失效（片段被删）报告 `uncovered_objectives` | `domain.validate_course` |
| **画面/字幕/说明同素材版本** | 每个 demo 片段三种角色绑定；`recheck_segment_bindings()` 检查版本集合是否唯一（`version_mismatch`） | `domain.recheck_segment_bindings` |
| **替换视频后超界时间码进入待修复** | follow 绑定自动跟随新版本，随后重校验：`timecode_out_of_bounds`（出点>时长）、`audio_missing`（视频无音轨）、`asset_version_missing` → `status='pending_repair'`；修复后重检通过才回 `ok` | `domain.recheck_asset_bindings / recheck_segment_bindings`，`app.replace_asset_version/repair_binding` |
| **固定课件版本复用 vs 模块跟随更新** | 导出时选 `pinned`（固化课程版本快照，可无限复用）或 `follow`（课程一改即标记 `stale`，需重建） | `packages.mode/stale`，`domain.bump_course` |
| **已开班课程冻结规则** | 开班即把包置 `frozen=1, mode='pinned'`；冻结包**禁止重建**(403)；模板升级不触碰其磁盘产物，班级访问的永远是旧版本快照 | `domain.start_class`，`app.rebuild_package` |
| **多人编辑版本条件冲突** | 每个实体有 `version`，更新须带 `base_version`（乐观锁）；不匹配返回 **409 + 当前值** | `domain.update_versioned` |
| **撤销不能回退同事后续修订** | 仅当实体仍停留在**该修订的版本**才允许撤销；一旦有后续修订（版本前移）→ **409**。删除章节的撤销会连同片段/绑定/覆盖/前置边快照一起恢复 | `domain.undo_revision / snapshot_chapter / restore_chapter` |
| **导出成功但回执丢失** | 课件产物、`export_receipts`、包状态在工作器**同一事务**提交；另提供管理员对账接口，按确定性回执号 `R{id:06d}-v{ver}` 幂等补发 | `worker.process`，`domain.build_package/reconcile_receipts` |
| **额度与重复导出一致** | `idempotency_key` 唯一：同键返回同包，配额只扣一次；额度用尽拒绝新键，但既有键重试仍幂等成功 | `domain.create_export`（条件 UPDATE 原子扣减） |
| **老师离开团队，历史仍可按权限查看** | 仅将 membership 置 `active=0`（不删用户/修订）；离老师写操作 403，其修订在工作区历史中保留；可重新加回 | `app.remove_member/add_member` |
| **训练目标改版** | `revision+1` 并记录 `objective_revisions`，既有覆盖置 `stale=1`，报告列入 `stale_coverage`，重新确认后清除 | `app.update_objective/confirm_coverage` |

## 校验报告（GET /api/courses/{id}/report）

```json
{
  "ok": false,
  "prereq_issues": [ {"type":"prereq_order|prereq_cycle|prereq_dangling", ...} ],
  "uncovered_objectives": [ {"code":"OBJ-2","revision":2, ...} ],
  "stale_coverage":      [ ... ],
  "pending_repairs":     [ {"role":"video","repair_reason":"audio_missing,..."} ],
  "version_skew":        [ {"segment_id":4,"bindings":"video@v2,subtitle@v1"} ],
  "total_minutes": 135.0
}
```

`ok=false` 时课程不应导出/发布；前端左侧栏实时呈现该报告。

## API 概览（节选）

- 工作区/成员：`GET /api/workspaces/{w}/members`、`POST .../members`、`DELETE .../members/{uid}`
- 课程/章节：`POST /api/courses/{c}/chapters`、`PUT /api/chapters/{id}`（带 `base_version`）、
  `POST /api/courses/{c}/chapters/reorder`、`DELETE /api/chapters/{id}`、`POST /api/chapters/{id}/prereqs`
- 片段：`POST /api/chapters/{id}/segments`、`PUT /api/segments/{id}`、`DELETE /api/segments/{id}`
- 目标：`POST .../objectives`、`PUT /api/objectives/{id}`（改版）、
  `POST /api/objectives/{id}/coverage`、`POST .../coverage/confirm`、`GET /api/courses/{id}/report`
- 素材：`POST /api/workspaces/{w}/assets`、`POST /api/assets/{id}/versions`（替换）、
  `POST /api/segments/{id}/bindings`、`PUT /api/bindings/{id}`（修复）
- 导出/包：`POST /api/courses/{id}/exports`（`mode` + `idempotency_key`）、
  `GET .../packages`、`GET /api/packages/{id}/artifact`（预览）、
  `POST /api/packages/{id}/rebuild`、`POST /api/workspaces/{w}/packages/reconcile`
- 开班：`POST /api/courses/{id}/classes`、`GET /api/classes/{id}`（看冻结内容）
- 历史/撤销：`GET /api/workspaces/{w}/revisions`、`POST /api/revisions/{id}/undo`

错误码：400 业务参数、403 权限/额度、404 不存在、409 乐观冲突/撤销冲突、500 内部错误。

## 后台工作器

轮询 `jobs` 表认领 `build_package` 作业：认领独立事务 → 构建（写产物 + 插回执 +
包转 done）同一事务提交 → 作业置 done；失败指数退避重试，三次后包/作业置 `failed`。
`Worker` 以守护线程随服务启动；也可调用 `server.worker.run_once(db, artifacts)` 同步处理。

## 九类验收情形的测试映射

见 `tests/test_acceptance.py`：删除前置章节、素材音轨缺失/时间码越界、训练目标改版、
导出成功但回执丢失、老师离开团队、权限化历史查看、未覆盖目标报告、额度与重复导出一致、
开班冻结；以及 `tests/test_domain_unit.py` 中的环检测、撤销恢复、工作器重试、
pinned/follow 过期逻辑等。
