<div align="center">

# EvoAgent

**让每次代码审查有证据，让每次反馈进入下一轮改进。**

![Python](https://img.shields.io/badge/Python-3.11-3776AB?logo=python&logoColor=white)
![Runtime](https://img.shields.io/badge/Runtime-Custom_Agent_Loop-0F766E)
![Storage](https://img.shields.io/badge/Storage-SQLite_%7C_PostgreSQL-4169E1)
![Integration](https://img.shields.io/badge/GitHub-Webhook_%2B_PAT-181717?logo=github)

[快速开始](#快速开始) · [审查一次变更](#审查一次变更) · [接入 GitHub](#接入-github) · [反馈与演进](#反馈与演进) · [测试](#测试)

</div>

![EvoAgent 工作台](docs/main.png)

EvoAgent 是一个个人代码审查项目：输入统一 Diff，Lead 分配任务，Security 与 Correctness/Reliability Worker 寻找缺陷，Critic 挑战候选结论，证据门禁决定哪些问题进入报告。它把审查、任务恢复、GitHub 评论和反馈演进串成一条可查看、可复现的流程。

- **有依据的报告**：保留原始候选、工具证据与门禁原因，逐项校验后再去重。
- **可恢复的执行**：自研 Runtime、持久化 checkpoint、取消、超时与累计 Token 预算；恢复复用已完成 Worker。
- **可验证的改进**：人工反馈进入案例库，Prompt/Skill 在冻结配置下回放，经评测与 smoke 后激活，支持回滚。

默认使用 SQLite 与进程内队列，可选 PostgreSQL、Redis Streams。运行范围是**单个服务实例**，实例内可并发执行任务。

## 工作流程

```mermaid
flowchart LR
    A[页面 / Diff API] --> S[ReviewService]
    G[GitHub PR Webhook] --> Q[异步队列]
    Q --> S
    S --> H[Runtime + Checkpoint]
    H --> L[Lead 分配]
    L --> W[Security / Reliability Workers]
    W --> C[Critic 审核候选]
    C --> F[Lead 选择 + 证据门禁 + 去重]
    F --> R[报告 / 可选 PR 评论]
    R --> B[人工反馈]
    B --> E[冻结配置回放 + 评测 + Smoke]
    E --> N[激活 / 回滚 Prompt 与 Skill]
    N --> L
```

普通审查包含 Lead 分配与最终综合、两个专业 Worker 和 Critic；高风险任务最多增加一轮 Worker 返工。必要角色未完成时任务为 `FAILED`，不会把执行故障当成“未发现问题”。

## 快速开始

以下为 Linux 本机运行流程。准备 Conda、Python 3.11，以及一个支持 OpenAI Chat Completions 的模型接口。公开源码包不包含数据库、管理员账户或模型密钥。

### 1. 创建环境

下载并解压项目后，进入包含本 README 的目录：

```bash
cd /path/to/EvoAgent
conda create -n evoagent python=3.11 -y
conda activate evoagent
python -m pip install -r requirements.txt
cp .env.example .env
```

### 2. 配置管理员与模型

生成登录签名密钥，将输出填入 `.env` 的 `EVOAGENT_AUTH_SECRET`：

```bash
python -c 'import secrets; print(secrets.token_urlsafe(48))'
```

编辑根目录 `.env`，至少填写以下配置。模型示例使用兼容接口上的 DeepSeek-V3.2；域名、API Key 和模型标识需与自己的服务商一致。

```dotenv
EVOAGENT_AUTH_REQUIRED=true
EVOAGENT_AUTH_SECRET=替换为上一步生成的随机密钥
EVOAGENT_BOOTSTRAP_ADMIN_USERNAME=admin
EVOAGENT_BOOTSTRAP_ADMIN_PASSWORD=替换为至少10个字符的自定义密码

EVOAGENT_LLM_PROVIDER=custom
EVOAGENT_LLM_BASE_URL=https://your-openai-compatible-endpoint.example/v1
EVOAGENT_LLM_API_KEY=替换为你的APIKey
EVOAGENT_LLM_MODEL=deepseek-ai/DeepSeek-V3.2

EVOAGENT_TOTAL_TOKEN_BUDGET=32000
EVOAGENT_AGENT_TOKEN_BUDGET=8000
EVOAGENT_TIMEOUT_SECONDS=240
```

占位符不能直接用于运行。新数据库会在首次启动时自动创建，并按上述配置创建 `default` 工作区的管理员。同名管理员已存在时，重启不会覆盖其密码；修改 bootstrap 密码不等于重置旧账户密码。

这里将任务超时设为 240 秒，适配本次真实模型接口的响应时间；代码默认值为 120 秒。调整等待时间不会增加 Token 额度。

`.env` 会在启动时加载，进程环境变量优先。修改配置后重启服务；保持在项目根目录启动，默认数据库与 Skills 路径相对于该目录。完整配置见 [.env.example](.env.example)。

### 3. 启动工作台

```bash
python -B -m evoagent
```

浏览器打开 **http://127.0.0.1:8080/**，使用刚设置的管理员账户登录。另开终端可检查服务：

```bash
curl --fail http://127.0.0.1:8080/health
```

未配置模型时可以启动工作台和健康检查；创建审查前必须完成模型配置。模型请求可能产生服务商费用。

## 审查一次变更

在工作台点击“新建 PR 审查”，仓库填写 `demo/api`，提交下面的 Diff：

```diff
--- a/average.py
+++ b/average.py
@@ -0,0 +1,3 @@
+def average(values):
+    """Return the average; return 0.0 when values is empty."""
+    return sum(values) / len(values)
```

这里的行为契约与实现冲突：空列表会除零。检查任务详情中的执行状态、Worker 候选、证据、门禁结果及最终报告；再将返回值改为 `sum(values) / len(values) if values else 0.0`，提交一次新审查对照。模型结论应结合证据检查，示例不承诺每次模型输出完全一致。

![审查结果与证据](docs/review.png)

### 如何理解结果

`state=SUCCESS` 表示执行完成。报告还会区分：

| 字段 | 含义 |
| --- | --- |
| `execution.review_status` | `complete` 或 `needs_review`；有候选被门禁拒绝时需要进一步检查 |
| `execution.review_outcome` | `clean`、`findings`、`partial` 或 `withheld` |
| `execution.finding_counts` | 原始候选、Lead 选择、门禁接受/拒绝及最终发布数量 |

门禁要求位置有效、必填字段完整、置信度达标，证据须精确对应新增代码行或本任务登记的工具事实。HIGH/CRITICAL 还需要修复和测试建议；精确代码证据本身可以支持 HIGH。全部候选被拒绝时风险为 `unknown`，不会显示绿色的“未发现问题”。审查覆盖新增行，删除、二进制与仅重命名不会被视为通过证明。

### 执行预算

| 配置 | 默认值 | 范围 |
| --- | ---: | --- |
| `EVOAGENT_TOTAL_TOKEN_BUDGET` | 32000 | 整项任务累计模型 Token |
| `EVOAGENT_AGENT_TOKEN_BUDGET` | 8000 | 每角色累计模型 Token，跨该角色的多轮调用共享 |
| `EVOAGENT_TIMEOUT_SECONDS` | 120 | 本次任务执行的总时间上限 |
| `EVOAGENT_MAX_STEPS` | 8 | Runtime 节点执行步数上限 |

输入与输出共同计入预算；Lead 分派、汇总、重试和恢复共享累计额度。上下文使用确定性压缩，预算充足时发送完整 Diff，超限时按完整代码行摘录并保留原始新旧行号。接口缺少 usage 时保守估算并标记来源；返回用量超限会记录真实消耗并终止。运行时把剩余时间传给模型和工具，预算耗尽后停止后续调用。

## 接入 GitHub

本机演示使用 **仓库 Webhook + Cloudflare Tunnel + fine-grained PAT**，不需要创建 GitHub App。

### 1. 配置 Secret 和 PAT

在 `.env` 中设置，并重启 EvoAgent：

```dotenv
EVOAGENT_GITHUB_WEBHOOK_SECRET=替换为独立生成的随机Secret
EVOAGENT_GITHUB_TOKEN=替换为目标仓库的fine-grained-PAT
EVOAGENT_AUTO_POST_REVIEW=true
```

Webhook Secret 可用前面的 `secrets.token_urlsafe(48)` 命令另行生成，不能与登录签名密钥共用。PAT 选择目标仓库，并按所用功能授权：

| 功能 | 仓库权限 |
| --- | --- |
| 读取私有仓库及 PR | `Contents: Read`、`Pull requests: Read` |
| 回写审查评论 | `Pull requests: Read and write` |
| 发布修复分支与 Draft PR | `Contents: Read and write`、`Pull requests: Read and write` |

只读公开仓库且不评论、不修复时可以不配置 PAT。`EVOAGENT_AUTO_POST_REVIEW` 默认关闭；开启后才会回写评论。

权限说明见 [GitHub 官方评论 API 文档](https://docs.github.com/en/rest/issues/comments)。

### 2. 开启公网转发

安装好 `cloudflared` 后，在另一个终端运行：

```bash
cloudflared tunnel --url http://127.0.0.1:8080
```

保留命令输出的 HTTPS 地址，同时保持 EvoAgent 与 Tunnel 运行。Quick Tunnel 重启后地址可能变化，需要同步更新 Webhook。该转发也会暴露工作台和 API，必须保持登录开启；长期使用可通过反向代理仅公开 Webhook 路径。

安装与命令说明见 [Cloudflare Quick Tunnel 文档](https://developers.cloudflare.com/tunnel/get-started/quick-tunnels/)。

### 3. 添加仓库 Webhook

进入目标仓库 **Settings → Webhooks → Add webhook**：

- **Payload URL**：`https://<Tunnel提供的域名>/webhooks/github`
- **Content type**：`application/json`
- **Secret**：与 `.env` 中的 Webhook Secret 一致
- **Events**：选择 **Pull requests**；保持 SSL 验证和 Active 开启

新建、重开 PR 或推送新提交后，Recent Deliveries 应显示 `202`，工作台出现异步任务。支持 `opened`、`reopened`、`synchronize`；其他 PR 动作会被忽略。

下载前后核对 head/base，报告绑定被审查版本；发布评论前再校验。PR 或比较基线已变化时标记 `publication_status=superseded`，保留旧报告。评论失败可以恢复发布，模型审查不会重跑；重复 Webhook delivery 复用原任务。

### 可选：验证修复补丁

默认保留修复建议。只有明确配置受信任源仓库和测试命令，报告包含 reviewed SHA，且 PR 版本未变时，才会验证补丁并创建 Draft PR：

```dotenv
EVOAGENT_TRUSTED_GITHUB_REPOSITORIES=owner/repository
EVOAGENT_REPAIR_TEST_COMMAND=python -m unittest discover -s tests
```

修复会执行语法检查、原始与修改后仓库测试，并保留 ZIP/Git 的普通文件可执行位。仓库测试不是安全沙箱，仅对自己控制的仓库启用；外来 fork 不继承目标仓库的执行信任。

## 反馈与演进

在任务详情中确认误报、漏报、坏修复或认可。认可反馈保留为已处理历史与记忆，不加入待处理失败案例。

新数据库的评测集为空。需要验证演进流程时，显式导入内置 smoke 样本：

```bash
python scripts/seed_evaluation_cases.py --builtin --db-path evoagent.db
```

内置样本只用于流程演示。真实效果需要自己维护带预期结论的 validation/holdout 案例；仓库中的 `pr_diff_100.jsonl` 是合成受控数据，不能当作真实 GitHub PR 准确率。

“演进实验室”支持 Prompt 操作；Agent Skill 使用 `/v1/skill-evolution/*` API。候选在冻结的模型、角色、Skills、预算和样本下回放，经门槛检查与 smoke 后激活；失败保留原版本，回滚恢复评测时的基线。已有任务与记忆不会被实验回放写入。

`skills/` 中的 `SKILL.md` 定义领域审查步骤与允许工具，Lead 按任务选用。修改后可在能力库重新加载，或调用 `POST /v1/skills/reload`。

## 常用接口

除健康检查与签名 Webhook 外，接口使用登录取得的 Bearer Token。

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| `POST` | `/v1/auth/login` | 获取 `access_token` |
| `POST` | `/v1/reviews` | 同步审查；加 `?async=true` 使用异步队列 |
| `GET` | `/v1/tasks/{id}` | 状态、执行记录与报告 |
| `GET` | `/v1/tasks/{id}/report` | Markdown 报告 |
| `POST` | `/v1/tasks/{id}/resume` | 恢复失败/中断任务，或重试失败评论 |
| `POST` | `/v1/tasks/{id}/cancel` | 取消任务；取消后不能恢复 |
| `POST` | `/v1/tasks/{id}/feedback` | 确认反馈 |
| `POST` | `/v1/tasks/{id}/fix` | 验证修复并按条件创建 Draft PR |
| `GET/POST` | `/v1/evaluation/cases` | 查询或增加标注案例 |
| `GET` | `/v1/evolution/status` | 模型与评测门槛就绪状态 |
| `GET` | `/metrics` | Prometheus 指标 |

死信列表为 `GET /api/queue/dead-letters`；使用记录中的 `payload.task_id` 调用任务恢复接口。成功 Worker 被复用，历史死信记录保留。

<details>
<summary><strong>可选 PostgreSQL、Redis 与 Docker Compose</strong></summary>

本机默认无需额外服务。需要外部后端时，可配置 `EVOAGENT_DATABASE_URL` 与 `EVOAGENT_REDIS_URL`，并保持单个 EvoAgent 服务实例。Redis 队列支持 ACK、执行中续租、断连重试与死信；`EVOAGENT_ASYNC_WORKERS` 控制实例内并发。

也可在配置好根目录 `.env` 的模型、签名密钥与管理员密码后运行：

```bash
docker compose up --build
```

根目录 `docker-compose.yml` 是唯一配置源，服务仅映射本机 `127.0.0.1:8080`，PostgreSQL 与 Redis 使用独立持久卷。容器中导入演示样本：

```bash
docker compose exec evoagent python scripts/seed_evaluation_cases.py --builtin
```

导入脚本不自动读取 `.env`；本机调用时应显式指定实际数据库，容器命令使用已配置的进程环境。

</details>

## 测试

本地回归使用临时数据库与模拟客户端，不加载项目 `.env`，也不访问当前业务数据库：

```bash
python -B scripts/run_tests.py
```

按需执行浏览器验收（需 Playwright/Chromium）与真实后端验收（使用专用本机测试服务）：

```bash
python -B tests/browser_smoke.py
python -B tests/run_backend_smoke.py --redis-url redis://127.0.0.1:6379/0 \
  --database-url postgresql://user:password@127.0.0.1:5432/test_database
```

