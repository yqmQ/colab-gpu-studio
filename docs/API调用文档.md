# Colab GPU Studio API 调用文档

- 文档版本：1.0.0
- 适用程序：`0.1.31-dev.8`
- 源码基线：`7b91190bed6fe5cea02d92c6ff039f64c7ebe497`（GitHub `main`）
- 仓库：<https://github.com/TizenryA/colab-gpu-studio>
- 面向：自写客户端、创作平台后端、Agent、自动化脚本。

> 这是项目自身的异步 HTTP API，**不是 OpenAI 兼容接口**。没有 `/v1/models`、`/v1/images/generations`、`/v1/chat/completions`。正确流程是：发现工作流 → 上传素材 → 提交批次 → 轮询 → 下载结果。
>
> 本文按以上源码核对，并使用隔离的本地 Mock 完成契约与示例验证。**未操作生产、未启动真实 GPU、未重新验证真实模型生成。** Mock 结果仅用于联调，不能视为 AI 真实出图。

## 1. 地址与安全边界

| 用途 | 地址 / 说明 |
|---|---|
| 自部署本地后端 | 默认 `http://127.0.0.1:8788` |
| 自部署公网后端 | `https://your-studio.example.com`，替换为自己的后端地址 |
| 在线演示 | 仅演示前端，不能代替生成后端 |
| 业务接口前缀 | 优先使用 `/api/gpu` |
| 素材接口前缀 | `/api/assets`，不是 `/api/gpu/assets` |
| 运行时 OpenAPI | `GET /openapi.json`；通常可用 `/docs` 查看交互文档 |

以下 `BASE` 是**服务根地址**，末尾不要加 `/api`、`/api/gpu` 或 `/v1`。

命令行示例使用 Bash、curl、jq、Python 3；Windows 可在 WSL/Git Bash 中执行，或直接使用第 10 节 Python 客户端。`<...>` 是需要替换的占位符，不是真实 ID 或凭据。

```bash
export BASE='http://127.0.0.1:8788'
read -rsp 'Studio API Token: ' STUDIO_API_TOKEN; printf '\n'
export STUDIO_API_TOKEN
```

凭据不得放进前端 JS、公开仓库、URL 查询参数或分享文档。`STUDIO_API_TOKEN` 是管理员级静态凭据，**不是可随意分发的普通用户 Key**。外部网页应通过自己的后端调用；当前应用没有通用跨域 CORS 接入配置，跨站写请求还会被 Origin 校验拒绝。

## 2. 鉴权

### 2.1 后端脚本：静态 Token

管理员在部署环境配置 `STUDIO_API_TOKEN`。客户端发送：

```http
Authorization: Bearer <STUDIO_API_TOKEN>
```

鉴权成功后映射为 `owner` 管理员，可访问此用户的项目、任务和素材。Token 为空或不匹配不会自动获得权限。

```bash
curl --fail-with-body -sS "$BASE/api/me" \
  -H "Authorization: Bearer $STUDIO_API_TOKEN"
```

`/api/me` 直接返回用户对象，不含 `data` 外壳。

### 2.2 密码登录：短期访问令牌

```http
POST /api/auth/login
Content-Type: application/json

{"password":"<站点密码>"}
```

成功直接返回以下结构，不含 `data` 外壳：

```json
{"access_token":"<会话令牌>","token_type":"bearer","expires_in":43200}
```

`expires_in` 默认 **43200 秒（12 小时）**。部署环境可用 `SESSION_TTL_SECONDS` 改成有限时长，允许范围 60–2592000 秒。30 天尾网登录设置为 `SESSION_TTL_SECONDS=2592000`。该值是上限，不是无限期；已签发的令牌不会因为后来改环境变量而延长。

后续发送 `Authorization: Bearer <access_token>`。登录同时设置 `studio_session` Cookie（`HttpOnly`、`SameSite=lax`、`Path=/`；`SITE_URL` 为 https 时带 `Secure`）。**Cookie 仍只作为 GET/HEAD 的鉴权回退**；写请求必须带 Bearer。不要只保存 Cookie 后直接 POST。

网页在刷新、新标签或重新打开后，如果页面里的令牌副本丢了，会带 Cookie 调用：

```http
GET /api/auth/session
```

有效会话直接返回与登录相同的字段（另有 `authenticated: true`），`expires_in` 是剩余秒数，**不会顺延原来的过期时间**，响应为 `Cache-Control: private, no-store`。带跨站 `Origin`、`Referer` 或 `Sec-Fetch-Site: cross-site` 的调用直接 403，响应里没有令牌。静态 `STUDIO_API_TOKEN` 不能用来换取这个浏览器令牌。没有这些浏览器头、但已经持有 Cookie 的非浏览器客户端仍能读到令牌，因为 Cookie 的值就是该令牌。

```http
POST /api/auth/logout
```

退出登录清除 Cookie。请求里带有有效浏览器会话时，已签发的会话令牌全部失效。无会话的调用只清 Cookie，不撤销别人的令牌。

登录有频率限制，`429 RATE_LIMITED` 时停止连续尝试。密码输入值、会话令牌均不得打印到共享日志。未打上会话保持补丁的 0.1.31-dev.8 没有 `/api/auth/session` 和 `/api/auth/logout`，时长固定为 43200 秒。

### 2.3 Agent 专用 Token

`GPU_AGENT_TOKEN` 与 `GPU_AGENT_OWNER_ID` 配合，仅由 Agent 入口识别：

- `GET /api/gpu/agent/tools`
- `POST /api/gpu/agent/invoke`

**不能拿 Agent Token 直接调用普通 `/api/gpu/batches` 或素材下载接口。** 真实生成集成优先由受信任后端调用批次 API，不要误把 Agent 入口当全功能 Key 系统。

## 3. 响应格式、错误与幂等

大部分业务 JSON 成功响应：

```json
{"data":{"id":"<资源ID>"},"error":null,"request_id":"<请求ID>"}
```

失败响应结构示意（不是实测失败记录）：

```json
{
  "data": null,
  "error": {"code":"IDEMPOTENCY_REQUIRED","message":"必须提供 Idempotency-Key，重试必须沿用原值","retryable":false},
  "request_id":"<请求ID>",
  "code":"IDEMPOTENCY_REQUIRED",
  "message":"必须提供 Idempotency-Key，重试必须沿用原值"
}
```

同时检查 HTTP 状态码和 `error`。每次请求响应头还带 `X-Request-ID`，用于服务端定位。

例外：`/api/health`、`/api/auth/login`、`/api/me` 直接返回对象；下载/缩略图返回二进制；OAuth 回调返回重定向；OpenAPI 是标准 OpenAPI JSON。

### 幂等规则

提交任务或批次时必须携带：

```http
Idempotency-Key: <本次业务请求唯一值，建议 UUID>
```

- Key 长度不超过 200 字符。
- **同一请求网络重试：原 Key + 完全相同的请求体。**
- 同 Key 同参数返回原任务/批次，不重复创建。
- 同 Key 不同参数返回 `409 IDEMPOTENCY_CONFLICT`。
- 用户明确“再生成一次”：新 Key。
- 上传素材、创建项目没有这项幂等保证，不能无脑自动重发。
- 收到 `202` 仅代表已入队，**不代表生成成功或预算校验最终通过**。

## 4. 接口速查

| 方法 | 路径 | 用途 |
|---|---|---|
| GET | `/api/health` | 健康与程序版本，无需登录 |
| GET | `/api/gpu/projects` | 项目列表 |
| POST | `/api/gpu/projects` | 新建项目 |
| PATCH | `/api/gpu/projects/{project_id}` | 修改项目名称 |
| GET | `/api/gpu/models` | 模型及可用状态 |
| GET | `/api/gpu/workflows?model_id=...` | 工作流版本列表 |
| GET | `/api/gpu/workflows/{workflow_id}/schema` | 参数 schema、默认值与估算 |
| GET / POST | `/api/assets` | 列出 / 上传素材 |
| GET | `/api/assets/{asset_id}/download` | 鉴权下载 |
| GET | `/api/assets/{asset_id}/preview` | 鉴权预览 |
| GET | `/api/assets/{asset_id}/thumbnail` | 图片缩略图 |
| POST | `/api/gpu/batches` | 提交批次，真实生成推荐入口 |
| GET | `/api/gpu/batches/{batch_id}` | 批次进度及各任务 |
| POST | `/api/gpu/batches/{batch_id}/cancel` | 取消整个批次 |
| POST | `/api/gpu/jobs` | 单任务提交（Mock / Colab，受准入约束） |
| GET | `/api/gpu/jobs?project_id=...` | 任务列表 |
| GET | `/api/gpu/jobs/{job_id}` | 任务详情、事件、阶段耗时、执行记录 |
| GET | `/api/gpu/jobs/{job_id}/events` | 事件 JSON 列表，不是 SSE |
| GET | `/api/gpu/jobs/{job_id}/outputs` | 产物素材列表 |
| POST | `/api/gpu/jobs/{job_id}/cancel` | 取消单任务 |
| POST | `/api/gpu/jobs/{job_id}/retry` | 新建一次生成，返回新任务 ID |
| GET / POST | `/api/gpu/jobs/{job_id}/attempts` | 执行记录 / 同任务重试，POST 当前限 Mock |
| GET | `/api/gpu/gallery?project_id=...` | 生成作品列表 |
| GET | `/api/gpu/settings` | 是否启用真实 GPU、OAuth、预算等 |
| GET | `/api/gpu/usage` | 用量与预算 |
| GET | `/api/gpu/leases` | 当前用户租约状态 |
| GET | `/api/gpu/compute/specs?provider=mock` | 模拟算力规格 |
| GET | `/api/gpu/compute/specs?provider=colab` | 真实规格，会访问 Colab，不会直接创建 runtime |
| GET / POST | `/api/gpu/presets` | 列出 / 保存工作流预设 |
| GET | `/api/gpu/loras?workflow_key=...` | 策展 LoRA 与 Drive 文件 |

部分业务路由也挂在 `/api` 别名下。新集成统一用上表，不依赖别名；完整机器可读路由及请求模型见随附 `openapi.json`。

## 5. 准备项目与发现工作流

### 5.1 创建或选择项目

```bash
curl --fail-with-body -sS "$BASE/api/gpu/projects" \
  -H "Authorization: Bearer $STUDIO_API_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"title":"API 创作项目"}' > project-response.json
export PROJECT_ID="$(jq -er '.data.id' project-response.json)"
```

成功为 `201`。已有项目可通过 `GET /api/gpu/projects` 获取，不必每次新建。

### 5.2 查询模型、版本、参数

```bash
curl --fail-with-body -sS "$BASE/api/gpu/models" \
  -H "Authorization: Bearer $STUDIO_API_TOKEN" > models.json
curl --fail-with-body -sS "$BASE/api/gpu/workflows" \
  -H "Authorization: Bearer $STUDIO_API_TOKEN" > workflows.json
jq '.data[] | {id,model_id,workflow_key,workflow_version,state,active}' workflows.json
```

选择规则：模型 `enabled=true`，工作流 `state=published`，优先使用 `active=true` 的版本；真实流程还必须完成后台审核、真实冒烟和 OAuth 授权。

**三个字段不要混用：**

- `id`：工作流记录 ID，供 `/workflows/{workflow_id}/schema` 使用。
- `workflow_key`：工作流逻辑名称，提交任务必填。
- `workflow_version`：该记录的版本，提交时**保持返回值的 JSON 类型**。例如 Mock 是数字 `1`，H3 Ref2V 可以是字符串 `"1.4.0"`，两者不能统一转字符串。

```bash
export WORKFLOW_ID='<从列表取得的工作流记录ID>'
curl --fail-with-body -sS "$BASE/api/gpu/workflows/$WORKFLOW_ID/schema" \
  -H "Authorization: Bearer $STUDIO_API_TOKEN" > schema.json
jq '.data.parameters' schema.json
```

`params` 的字段名、类型、范围、选项、必填项均以 schema 为准；不是所有模型都接受 `steps`、`width`、`height` 或 `sampler`。不要把 OpenAI 的 `size`、`n`、`response_format` 直接塞进来。

## 6. 上传与绑定参考素材

```bash
curl --fail-with-body -sS "$BASE/api/assets" \
  -H "Authorization: Bearer $STUDIO_API_TOKEN" \
  -F "project_id=$PROJECT_ID" \
  -F 'file=@./reference.png' > asset-response.json
export ASSET_ID="$(jq -er '.data.id' asset-response.json)"
```

成功为 `201`；请求必须是 `multipart/form-data`，字段为 `project_id` 和 `file`，不要手写 multipart 的 boundary。

- 默认单文件上传限制为 100 MiB，可由部署配置调整；反代也可能有更小限制。
- 素材存储支持 PNG/JPEG/WebP/GIF、MP4/WebM、WAV/MP3，但**工作流 image 输入仅接受 PNG/JPEG/WebP**。
- 参考图必须与任务属于同一项目。
- **只填 `asset_ids` 不会自动绑定到图像输入。** 必须同时把素材 ID 放入工作流的图像参数，如 `params.ref_1`。
- 图像输入会转为 JPEG；所有参考图压缩后合计不得超过 400 KiB，超限会报参数错误。这与上传限制是两回事。
- H3 Ref2V 1.4.0 暴露 `ref_1` 至 `ref_6`，不是底层模型理论支持的任意数量参考。

列出可用素材：

```bash
curl --fail-with-body -sS --get "$BASE/api/assets" \
  -H "Authorization: Bearer $STUDIO_API_TOKEN" \
  --data-urlencode "project_id=$PROJECT_ID"
```

## 7. 提交生成

### 7.1 通用 JobInput

| 字段 | 必填 | 说明 |
|---|---|---|
| `project_id` | 是 | 当前用户项目 ID |
| `model_id` | 是 | 与工作流一致的模型标识 |
| `workflow_key` | 是 | 工作流逻辑名称 |
| `workflow_version` | 是 | 原样使用工作流返回的数字或字符串 |
| `params` | 否 | schema 定义的参数对象，默认 `{}` |
| `asset_ids` | 否 | 相关素材 ID，最多 10 项；不替代图像参数绑定 |
| `loras` | 否 | LoRA 列表，见第 11 节 |
| `preset_id` | 否 | 必须属于同一工作流记录 |
| `execution_policy` | 否 | 默认 Mock；生产建议显式传入 |

JobInput、执行策略、BatchInput 都禁止额外字段，拼错名称可能返回 `422`。

`execution_policy`：

```json
{"provider":"mock","shutdown_after_job":true,"max_runtime_minutes":60,"idle_grace_minutes":0}
```

- `provider` 仅支持 `mock` / `colab`。
- 当前强制 `shutdown_after_job=true`、`idle_grace_minutes=0`，不能靠客户端延长闲置。
- `max_runtime_minutes` 请求范围 1–1440，默认 60；**实际还受服务器 `GPU_MAX_RUNTIME_MINUTES` 上限约束**，填 120 不保证得到 120。应先查询 `/settings` 或 `/usage`。
- 批次可复用兼容 runtime，因此某个子任务完成并不意味着整个批次已释放 GPU。
- GPU 类型来自已审查的工作流 `compute_spec`，不是在普通 JobInput 顶层随便填 `gpu`。

### 7.2 最小 Mock 单任务

以下只生成显式模拟产物，不会开真实 GPU：

```bash
jq -n --arg project "$PROJECT_ID" '{
  project_id:$project,
  model_id:"demo-mock",workflow_key:"demo-mock",workflow_version:1,
  params:{prompt:"API mock test",steps:4,seed:42},
  execution_policy:{provider:"mock",shutdown_after_job:true,max_runtime_minutes:60,idle_grace_minutes:0}
}' > mock-job.json
export REQUEST_KEY="$(python3 -c 'import uuid; print(uuid.uuid4())')"
curl --fail-with-body -sS "$BASE/api/gpu/jobs" \
  -H "Authorization: Bearer $STUDIO_API_TOKEN" \
  -H "Idempotency-Key: $REQUEST_KEY" \
  -H 'Content-Type: application/json' \
  --data-binary @mock-job.json > job-response.json
export JOB_ID="$(jq -er '.data.id' job-response.json)"
```

成功为 `202`，`data.id` 与 `data.job_id` 相同。后端 worker 必须在运行，否则任务不会因为客户端轮询而自动推进。

### 7.3 真实生成：推荐使用批次接口

**真实 Colab 可以通过 `/api/gpu/jobs` 提交单任务，也可以通过 `/api/gpu/batches` 提交批次。** 两者都必须满足真实模式、OAuth、工作流审核、模型可用性和预算约束；不是单纯改 `provider` 就能开机。

本文和 Python 示例统一使用批次接口，便于扩展到多条任务与复用兼容 runtime。**当选择真实批次接口时，才强制要求 `mode="batch"`。** 这个约束不能误读成“单任务接口禁止真实生成”。

批次结构：

```json
{
  "mode":"batch",
  "idle_seconds":0,
  "jobs":["<这里放完整 JobInput 对象，不是字符串>"]
}
```

上面仅解释层级；可直接提交的 H3 示例见下方。`jobs` 为 1–100 项；`idle_seconds` 为 0–300，默认 120。真实批次必须全为 `colab`，不能混入 Mock，也不能使用 `save` / `smart`。兼容的任务按队列顺序共用 runtime；是否复用取决于服务器兼容性指纹，**不是保证一个批次永远只开一台机器**。

### 7.4 H3 Ref2V 1.4.0 完整请求

前提：已经上传参考图；当前部署确实有已发布、已审核的该版本，模型可用，真实 GPU 已启用、OAuth 有效、预算足够。

> **以下 POST 会提交真实付费任务。仅在明确需要生成时执行。** 本文编写与验证过程中没有执行真实 POST。

```bash
jq -n --arg project "$PROJECT_ID" --arg image "$ASSET_ID" '{
  mode:"batch",idle_seconds:0,
  jobs:[{
    project_id:$project,
    model_id:"minimax-h3",
    workflow_key:"minimax-h3-ref2v",
    workflow_version:"1.4.0",
    asset_ids:[$image],
    params:{
      ref_1:$image,
      prompt:"<Picture 1> The character turns their head and smiles. Audio: gentle wind.",
      mode:"fast8",width:544,height:960,length:124,seed:42
    },
    execution_policy:{provider:"colab",shutdown_after_job:true,max_runtime_minutes:60,idle_grace_minutes:0}
  }]
}' > h3-batch.json
export REQUEST_KEY="$(python3 -c 'import uuid; print(uuid.uuid4())')"
curl --fail-with-body -sS "$BASE/api/gpu/batches" \
  -H "Authorization: Bearer $STUDIO_API_TOKEN" \
  -H "Idempotency-Key: $REQUEST_KEY" \
  -H 'Content-Type: application/json' \
  --data-binary @h3-batch.json > batch-response.json
export BATCH_ID="$(jq -er '.data.id' batch-response.json)"
jq '.data | {id,state,total_jobs,job_ids,mode}' batch-response.json
```

`POST` 的返回含 `data.id`（批次 ID）、`job_ids`（子任务 ID 列表）、`total_jobs`、`mode`、`policy_snapshot`。**不要指望提交响应就有展开的 `jobs`**，请随后 GET 批次详情。

H3 Ref2V 1.4.0 参数摘要（仍以当前 schema 为准）：

| 参数 | 说明 |
|---|---|
| `ref_1` | 必填参考图素材 ID；另有可选 `ref_2`…`ref_6` |
| `prompt` | 可用 `<Picture 1>` 等按顺序引用；声音可写 `Audio:` |
| `mode` | `fast`、`fast8`、`refine`、`refine8`、`hq`；默认 `fast8` |
| `width` / `height` | 256–1088，按 schema 要求使用 32 倍数；默认 544×960 |
| `length` | 帧数，不是秒数；范围 22–362，示例为 124 |
| `seed` | 固定种子可复现；schema 默认 `-1` 表示随机，返回查看 `resolved_seed` |
| `video_vae` | VAE 文件名枚举；省略采用工作流默认值，不传标签文本 |

## 8. 查询进度与下载

### 8.1 批次与任务详情

```bash
curl --fail-with-body -sS "$BASE/api/gpu/batches/$BATCH_ID" \
  -H "Authorization: Bearer $STUDIO_API_TOKEN" > batch-status.json
jq '.data | {id,state,total_jobs,finished_jobs,counts,jobs}' batch-status.json
```

批次状态：`queued`、`running`、`completed`、`failed`、`cancelled`、`partial`。混合成功/失败/取消时可能为 `partial`，应逐个查看 `jobs`，保存成功产物。

单任务活动状态：

```text
queued → validating → waiting_for_capacity → allocating
→ preparing / loading → running → uploading → cleaning_up
```

清理未完成还会进入 `cleanup_pending`。以上为典型流程，阶段可能被跳过或恢复，不保证逐一出现。

单任务终态：`completed`、`failed`、`cancelled`。`cleanup_pending` **不是终态**。取消后也可能先进行清理。

```bash
export JOB_ID='<子任务ID>'
curl --fail-with-body -sS "$BASE/api/gpu/jobs/$JOB_ID" \
  -H "Authorization: Bearer $STUDIO_API_TOKEN"
curl --fail-with-body -sS "$BASE/api/gpu/jobs/$JOB_ID/events" \
  -H "Authorization: Bearer $STUDIO_API_TOKEN"
```

详情重点字段：`state`、`stage`、`progress`、`error_code`、`error_message`、`resolved_seed`、`resolved_params`、`estimated_units`、`actual_units`、`mock`、`runtime_deleted`、`events`、`timings`、`attempts`。部分字段按阶段出现；`progress` 若存在使用 0–1 比例，不是 0–100。

建议每 5–10 秒轮询，网络失败退避；不要高频循环。客户端超时或关闭手机不会自动取消后端任务。

### 8.2 获取产物列表

```bash
curl --fail-with-body -sS "$BASE/api/gpu/jobs/$JOB_ID/outputs" \
  -H "Authorization: Bearer $STUDIO_API_TOKEN" > outputs.json
jq '.data[] | {id,filename,mime,size,sha256}' outputs.json
```

`data` 是素材数组，不是 OpenAI 的 `url` / `b64_json`。没有产物时可能为空数组，不能仅以 HTTP 200 判定生成成功。

### 8.3 鉴权下载

```bash
export OUTPUT_ID="$(jq -er '.data[0].id' outputs.json)"
curl --fail-with-body -sS "$BASE/api/assets/$OUTPUT_ID/download" \
  -H "Authorization: Bearer $STUDIO_API_TOKEN" \
  -o result.bin
```

下载路径同样需要鉴权，不是永久公共直链。使用素材 `mime`、`filename` 选择正确扩展名；这里 `result.bin` 仅避免把未知文件误写成图片。建议对照 `size` 和 `sha256` 验证完整性，附带 Python 示例已自动执行。

页面 `<img>` 无法自行附加任意 Bearer 头；自建客户端应通过后端转发或前端鉴权 fetch 后转 Blob URL，不能把管理员 Token 拼在图片 URL 上。

单任务 `completed` 不保证该批次 runtime 已删除，可能正转给下一任务。需要核对释放时查询 `/api/gpu/leases`，确认对应批次无未删除租约；不要仅凭 `runtime_deleted` 一个子任务字段判断全局释放状态。

## 9. 取消、再次生成与执行重试

```bash
curl --fail-with-body -sS -X POST "$BASE/api/gpu/jobs/$JOB_ID/cancel" \
  -H "Authorization: Bearer $STUDIO_API_TOKEN"
curl --fail-with-body -sS -X POST "$BASE/api/gpu/batches/$BATCH_ID/cancel" \
  -H "Authorization: Bearer $STUDIO_API_TOKEN"
```

取消可能先设置请求标记，再由 worker 清理；继续查询直到任务终态，并检查相关租约。取消不能退还已经消耗的计算单元。

### 三种“重试”不能混淆

| 操作 | 语义 | 注意 |
|---|---|---|
| 原 POST 同 Key 同 body | 网络重放 | 返回原资源，不是再生成 |
| `POST /jobs/{id}/retry` + 新 Key | 重新生成 | 新任务 ID；原任务需已终结，沿用已解析种子 |
| `POST /jobs/{id}/attempts` + 新 Key | 原 job 内再次执行 | 当前只支持清理完成的失败/取消 Mock；真实任务不可用 |

真实单任务可以使用 `/retry`，仍会重新执行模型、工作流、OAuth 等准入检查并接受预算约束；它创建新任务，不是恢复原执行。需要保持批次复用语义时，以原参数重新提交**新批次 + 新幂等键**。特殊诊断/预热任务需回原确认入口，不能当普通生成重试。

## 10. 可直接运行的 Python 示例

随附 `examples/studio_api_example.py`，功能包括：

- 发现可用模型和已发布工作流，保持版本类型。
- 可选择已有项目，或创建示例项目。
- H3 可选上传参考图并填入 `ref_1`。
- 统一走批次 API；**默认 Mock**。
- 请求体与幂等键先写状态文件，网络失败可恢复。
- 轮询批次、逐项下载并校验大小和 SHA-256。
- 不记录 Token；不自动重定向到其他源。

在文档包根目录执行：

```bash
python3 -m venv .venv
.venv/bin/python -m pip install 'httpx>=0.27,<1'
export STUDIO_BASE_URL='http://127.0.0.1:8788'
read -rsp 'Studio API Token: ' STUDIO_API_TOKEN; printf '\n'; export STUDIO_API_TOKEN
.venv/bin/python examples/studio_api_example.py
```

已有 `uv` 也可以免建显式环境：

```bash
uv run --with 'httpx>=0.27,<1' python examples/studio_api_example.py
```

默认运行 Mock；使用前需先启动自己的完整后端与 worker。服务端必须具有 Mock 模型/模板，演示静态站不可用。

H3 参数文件 `h3-params.json` 示例：

```json
{"prompt":"<Picture 1> The character turns their head and smiles. Audio: gentle wind.","mode":"fast8","width":544,"height":960,"length":124,"seed":42}
```

**明确允许真实付费生成时**，另选状态文件执行：

```bash
.venv/bin/python examples/studio_api_example.py \
  --real --workflow-key minimax-h3-ref2v \
  --project-id '<已有项目ID>' \
  --reference ./reference.png --params ./h3-params.json \
  --max-runtime 60 --state ./h3-api-state.json --output ./h3-results
```

恢复查询时保留相同地址、状态文件、`--real`（若原任务为真实）再执行。**已有状态文件时以保存的请求体为准，修改命令行参数不会重写已提交任务。** 想生成新任务应使用新的 `--state` 文件。状态文件不含 Token，但含提示词/素材 ID，应视作私人业务数据。

脚本本地等待超时不会自动取消后台任务；默认等待 7200 秒，可用 `--wait-seconds` 调整。整个批次失败/部分失败会返回非零退出码，成功子任务产物仍保存。

## 11. 预设、LoRA 与 Agent

### 预设

```http
POST /api/gpu/presets
Content-Type: application/json

{"name":"我的预设","workflow_version_id":"<工作流记录ID>","overrides":{"seed":42}}
```

成功 `201`。提交 JobInput 时填写返回的 `preset_id`；预设必须属于同一工作流记录，不能只因名称相同就跨版本复用。

### LoRA

先查询 `/api/gpu/loras?workflow_key=minimax-h3-ref2v`。`data.curated` 是内置候选，`data.files` 为用户 Drive 文件；Drive 不可用时可能仍返回策展列表并带 `data.error`，不要将 HTTP 200 视作 Drive 已连通。

在支持的 H3 工作流中，JobInput 可添加：

```json
{"loras":[{"id":"<列表中取得的ID>","strength":0.8}]}
```

允许数量以工作流/服务限制为准，强度范围 `-3.0` 至 `3.0`。不支持 LoRA 的工作流会拒绝。上传入口 `POST /api/gpu/loras/upload` 接收 `{"name":"example.safetensors","size":12345}`，返回的是 Drive 可续传会话 `upload_url`，**不是直接上传完成**；涉及 Google Drive 授权及专门的续传协议，不属于普通 `/api/assets` 上传。不要把 Studio 管理员 Token 转发到返回的外部上传 URL。

### Agent 调用

```http
POST /api/gpu/agent/invoke
Authorization: Bearer <GPU_AGENT_TOKEN>
Content-Type: application/json

{"tool":"workflow.list","arguments":{}}
```

先 GET `/api/gpu/agent/tools` 发现白名单。常用：`workflow.list`、`model.list`、`job.get`、`job.cancel`、`job.list_logs_redacted`、`usage.inspect`。

`job.create` 在 `arguments` 内放完整 JobInput，加 `idempotency_key`，不是靠 HTTP 头传入。当前 Agent 白名单**没有 batch.create**，但 `job.create` 可以在满足相同准入要求时提交真实单任务。Agent Token 不能调用普通素材下载接口，所以不要宣称它已覆盖上述批次与下载的完整流程；可由受信任后端负责批次提交与鉴权下载。Agent 无任意 shell、预算修改或直接 runtime 开机权限。

## 12. 预算、错误排查与集成建议

### 预算

- `GET /api/gpu/settings`：检查 `real_enabled`、Google 连接及预算。
- `GET /api/gpu/usage`：检查 `unit_rate`、`daily_limit`、`monthly_limit`、`max_job_units`、`max_runtime_minutes`、`active_runtimes`。
- `rate_unit` 为每分钟计算单元，**不是美元/分钟**。
- `estimated_units` 只是估算；预算可能按最大运行时间预留，预计能跑完不代表一定能通过预算。
- `actual_units=null` 表示没有实际值，不应强行改成 0；Google 实际扣费以账户记录为准。
- Mock 不产生真实计算消费，不能拿 Mock 用量证明真实成本。

### 常见错误

| 错误码 / 情形 | 处理 |
|---|---|
| `401 UNAUTHENTICATED` | 核对 Bearer、Token 类型、会话是否过期 |
| `403 CSRF_ORIGIN` | 不从异源浏览器直接写入；使用自己的服务端调用 |
| `IDEMPOTENCY_REQUIRED` | 补 Key；Agent job.create 则放 arguments |
| `409 IDEMPOTENCY_CONFLICT` | 同 Key 参数已改变；真正新请求才换 Key |
| `503 SCHEDULER_BUSY` | 退避后沿用原 Key/原 body 重试 |
| `REAL_GPU_DISABLED` | 当前实例未开启真实模式；不同入口 HTTP 状态可能不同 |
| `MODEL_UNAVAILABLE` / `WORKFLOW_UNPUBLISHED` | 重新发现模型、版本及发布状态 |
| `REVIEW_REQUIRED` | 后台完成审查及真实冒烟；不能客户端伪造通过 |
| `GOOGLE_NOT_CONNECTED` | 在站点设置完成 Google 授权 |
| `RATE_UNKNOWN` | 管理员配置可信估算费率，不能靠请求参数覆盖 |
| `POLICY_INVALID` / `POLICY_RESTRICTED` | 真实批次使用 mode=batch；不要改强制释放策略 |
| `EXECUTOR_MISMATCH` | Mock 模型只能 Mock，真实模型必须 colab |
| `ASSET_PROJECT_MISMATCH` | 使用同一项目上传的素材 |
| `PRESET_INCOMPATIBLE` | 预设必须属于本次工作流记录 |
| `422 VALIDATION_ERROR` | 对照 schema；检查多余字段、类型与参考图压缩限制 |
| `413` | 上传/请求过大；同时核查后端与反代限制 |
| 任务长时间 `queued` | 查询 worker/调度服务，不要重复创建新任务 |
| `cleanup_pending` | 继续跟踪租约清理，不能当生成已彻底结束 |
| HTTP 200 但内容是 HTML | BASE 可能指向静态演示、错误前缀或前端 fallback |

错误码也可能只出现在已受理任务的 `error_code` 中，而不是 POST 的即时 HTTP 错误里。

### 推荐接入方式

```text
手机/前端 → 你的业务后端 → GPU Studio API
                         ↓
                保存 batch_id / job_id
                         ↓
                轮询 → 鉴权下载 → 转发产物
```

1. 后端持有密钥，浏览器不接触管理员 Token。
2. 业务数据库保存请求体、幂等键、批次 ID、任务 ID。
3. 每次调用先发现工作流，不把所有模型参数硬编码成同一套。
4. 推荐真实生成统一用批次接口；单任务接口也可用，但不提供同批复用编排。
5. 重试网络不重复花费；明确再生成才换 Key。
6. 下载检查大小/哈希，失败任务保存 `request_id` 和事件。
7. 本版本没有对外完成 Webhook / 生成 SSE 协议，使用轮询，不虚构推送接口。

## 13. 文档依据与验证范围

权威顺序：正在运行版本的代码/OpenAPI → 当前工作流 schema → 本文基线。升级后先 GET `/api/health`，核对版本；不要仅看网站标题。

源码定位：

- `api/app/main.py`：登录、项目、工作流、素材、任务、输出、Agent 与路由前缀。
- `api/app/common.py`：Bearer/Cookie 鉴权与响应外壳。
- `api/app/job_schemas.py`：JobInput 与执行策略。
- `api/app/batch_routes.py`：批次校验、幂等、查询、取消与租约。
- `api/app/admission.py`：工作流/模型/OAuth/参数/预算估算准入。
- `api/app/gpu/compute.py`、`scheduler.py`：真实执行限制、阶段、预算、租约释放。
- `api/app/gpu/repository.py`：批次返回、同任务 attempts 及持久化。
- `api/app/gpu/inputs.py`、`api/app/storage.py`：素材格式、参考图绑定及大小限制。
- `api/app/gpu/templates/builtin/minimax-h3-ref2v-1.4.0.json`：本文 H3 参数。

随包 `api-evidence/` 提供 `验证记录.json` 与对应基线导出的 `openapi.json`。需要复核时，在完整源码已安装测试依赖的环境执行：

```bash
DOCS_SOURCE_ROOT=/path/to/colab-gpu-studio \
  /path/to/colab-gpu-studio/.venv/bin/python examples/verify_api_docs.py
```

核验脚本需要完整项目源码及其 FastAPI/TestClient/Pillow 等依赖，不是独立的线上探测脚本。它不读取生产 `.env`，只使用内存数据库和固定的非秘密测试凭据。验证采用本地内存数据库、临时素材目录、Mock Provider，并阻断真实出站网络；只证明请求、鉴权、幂等、状态查询及下载协议正确，**不证明线上部署状态、Google 授权、真实模型质量、实际扣费或真实 GPU 释放**。
