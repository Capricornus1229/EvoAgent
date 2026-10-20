<a name="top"></a>

<div align="center">
  <h1>EvoAgent</h1>
  <p><strong>多 Agent 代码审查与反馈演进工作台</strong></p>
  <p>从变更发现问题，以证据整理结论，用反馈积累经验。</p>
  <p>
    <img src="https://img.shields.io/badge/Python-3.11-3776AB?style=flat-square&amp;logo=python&amp;logoColor=white" alt="Python 3.11">
    <img src="https://img.shields.io/badge/Runtime-Custom_Agent_Loop-0F766E?style=flat-square" alt="Custom Agent Runtime">
    <img src="https://img.shields.io/badge/Storage-SQLite_%7C_PostgreSQL-4169E1?style=flat-square" alt="SQLite / PostgreSQL">
    <img src="https://img.shields.io/badge/GitHub-Webhook-181717?style=flat-square&amp;logo=github" alt="GitHub Webhook">
    <a href="LICENSE"><img src="https://img.shields.io/badge/License-MIT-246857?style=flat-square" alt="MIT License"></a>
  </p>
  <p>
    <a href="#quickstart"><strong>快速开始 →</strong></a>&nbsp;&nbsp; · &nbsp;&nbsp;
    <a href="#features">核心能力</a>&nbsp;&nbsp; · &nbsp;&nbsp;
    <a href="#architecture">协作架构</a>&nbsp;&nbsp; · &nbsp;&nbsp;
    <a href="#review">审查示例</a>&nbsp;&nbsp; · &nbsp;&nbsp;
    <a href="#github">GitHub 接入</a>&nbsp;&nbsp; · &nbsp;&nbsp;
    <a href="#evolution">反馈与演进</a>
  </p>
</div>

<p align="center">
  <a href="docs/main.png"><img src="docs/main.png" width="100%" alt="EvoAgent 工作台：审查任务、Agent 协作与运行概览"></a>
</p>
<p align="center"><sub>从工作台发起审查，查看角色分工、代码证据与任务执行记录。</sub></p>

**EvoAgent** 将多 Agent 审查、代码证据校验、GitHub 评论与反馈演进连接到同一个工作台。Lead 拆分任务，专业 Worker 并行检查变更，Critic 复核候选结论，再由 Lead 综合、门禁校验与去重生成报告。

可以直接粘贴 Diff 发起审查，也可以连接 GitHub PR 自动触发。每个任务保留执行轨迹、模型调用、Token 用量与提交版本；审查中的反馈进一步用于记忆积累和 Prompt / Skill 迭代。

---

<a name="features"></a>

## ✨ 核心能力

<table>
  <tr>
    <td width="50%" valign="top">
      <h3>🧭 多 Agent 协作</h3>
      <p>Lead 分派任务，Security 与 Correctness / Reliability Worker 并行审查，Critic 复核候选，高风险任务支持一轮返工。</p>
    </td>
    <td width="50%" valign="top">
      <h3>🔎 代码证据门禁</h3>
      <p>保留原始候选，校验新增行位置、代码证据、置信度与修复建议，逐条验证后合并重复结论。</p>
    </td>
  </tr>
  <tr>
    <td width="50%" valign="top">
      <h3>📚 上下文与预算</h3>
      <p>按风险选择代码上下文，压缩时保留原始行号；输入与输出共用任务 Token 池，角色多轮调用累计计量。</p>
    </td>
    <td width="50%" valign="top">
      <h3>⏯️ 可恢复的执行</h3>
      <p>自研 Runtime 保存 checkpoint，支持取消、超时和失败恢复，复用已完成的 Worker 与执行结果。</p>
    </td>
  </tr>
  <tr>
    <td width="50%" valign="top">
      <h3>🔗 GitHub 工作流</h3>
      <p>Webhook 触发异步审查，报告绑定 PR 提交版本，支持评论回写、发布重试与经过测试的修复 Draft PR。</p>
    </td>
    <td width="50%" valign="top">
      <h3>🧠 反馈与演进</h3>
      <p>反馈沉淀为审查记忆与改进信号，Prompt / Skill 候选经回放评测后激活，支持版本查看与回滚。</p>
    </td>
  </tr>
</table>

<a name="architecture"></a>

## 🧩 协作架构

### 四类角色，各有分工

| 角色 | 职责 | 主要输出 |
| --- | --- | --- |
| **Lead** | 拆分审查任务、选择 Skills、综合结论 | Worker 分派、返工要求、最终候选与重复分组 |
| **Security** | 检查输入处理、权限、注入与敏感信息 | 安全问题、代码证据、修复和测试建议 |
| **Correctness / Reliability** | 检查行为契约、边界条件与异常路径 | 正确性和可靠性问题、复现线索与建议 |
| **Critic** | 复核候选依据、位置与推理 | 接纳意见、反驳理由与置信度调整 |

### 从一次变更到下一轮改进

```mermaid
flowchart TD
    UI[工作台 / Diff API] --> Runtime[任务 Runtime + Checkpoint]
    GH[GitHub PR Webhook] --> Queue[异步队列]
    Queue --> Runtime
    Runtime --> Lead[Lead 分派任务]
    Lead --> Security[Security Worker]
    Lead --> Reliability[Correctness / Reliability Worker]
    Runtime --> Scanner[规则扫描]
    Security --> Candidates[保留原始候选]
    Reliability --> Candidates
    Scanner --> Candidates
    Candidates --> Critic[Critic 复核]
    Critic --> Final[Lead 综合]
    Final --> Gate[逐条证据门禁]
    Gate --> Merge[合并重复结论]
    Merge --> Report[审查报告 / GitHub 评论]
    Report --> Feedback[人工反馈与审查记忆]
    Feedback --> Improve[生成 Prompt / Skill 候选]
    Improve --> Replay[回放评测 + Smoke]
    Cases[(标注评测集)] --> Replay
    Replay --> Versions[激活 / 回滚版本]
    Versions -. 后续任务 .-> Lead

    classDef entry fill:#eaf6f3,stroke:#278b78,color:#164b42
    classDef agent fill:#eff5fa,stroke:#668da9,color:#29485e
    classDef evidence fill:#f7f4ea,stroke:#b09b61,color:#5d4e26
    classDef evolution fill:#f1eef9,stroke:#9280b5,color:#55436f
    class UI,GH,Queue,Runtime,Report entry
    class Lead,Security,Reliability,Critic,Final agent
    class Scanner,Candidates,Gate,Merge evidence
    class Feedback,Improve,Cases,Replay,Versions evolution
```

审查由任务 Runtime 管理步骤、重试和 checkpoint；角色通过独立指令、工具范围与 Skills 协作。默认采用 **SQLite + 进程内队列**，也可使用 **PostgreSQL + Redis Streams**。

<p align="right"><a href="#top">↑ 返回顶部</a></p>

<a name="quickstart"></a>

## ⚡ 快速开始

准备 **Linux、Conda、Python 3.11**，以及可用的 OpenAI Chat Completions 兼容模型接口。

### 1 · 获取项目与创建环境

```bash
git clone https://github.com/Capricornus1229/EvoAgent.git
cd EvoAgent

conda create -n evoagent python=3.11 -y
conda activate evoagent
python -m pip install -r requirements.txt

test -f .env || cp .env.example .env
```

### 2 · 配置模型与管理员

生成登录签名密钥，将输出填入 `.env` 的 `EVOAGENT_AUTH_SECRET`：

```bash
python -c 'import secrets; print(secrets.token_urlsafe(48))'
```

编辑根目录 `.env`，填写自己的接口地址、API Key、管理员密码与刚生成的密钥。下面使用 custom 接口上的 DeepSeek-V3.2 作为示例，模型标识按服务商配置填写：

```dotenv
EVOAGENT_AUTH_REQUIRED=true
EVOAGENT_AUTH_SECRET=your_generated_auth_secret
EVOAGENT_BOOTSTRAP_ADMIN_USERNAME=admin
EVOAGENT_BOOTSTRAP_ADMIN_PASSWORD=your_admin_password

EVOAGENT_LLM_PROVIDER=custom
EVOAGENT_LLM_BASE_URL=https://your-model-provider.example/v1
EVOAGENT_LLM_API_KEY=your_api_key
EVOAGENT_LLM_MODEL=deepseek-ai/DeepSeek-V3.2

EVOAGENT_TOTAL_TOKEN_BUDGET=32000
EVOAGENT_AGENT_TOKEN_BUDGET=8000
EVOAGENT_TIMEOUT_SECONDS=240
```

首次启动会自动建库，并创建 `default` 工作区的管理员。管理员密码至少 10 个字符；上面的示例为整次审查预留 240 秒。完整配置见 [.env.example](.env.example)。

### 3 · 启动与登录

在项目根目录运行：

```bash
python -B -m evoagent
```

另开终端检查服务：

```bash
curl -fsS http://127.0.0.1:8080/health
```

返回 `status: "ok"` 后，打开 **http://127.0.0.1:8080/**，使用配置的管理员账户登录。

| 入口 | 地址 |
| --- | --- |
| 审查工作台 | `http://127.0.0.1:8080/` |
| 健康检查 | `http://127.0.0.1:8080/health` |

配置在启动时加载，修改 `.env` 后重启服务。

<a name="review"></a>

## 🔎 审查一次变更

进入工作台的 **发起审查**，仓库填写 `demo/api`，粘贴下面的 Diff：

```diff
--- /dev/null
+++ b/average.py
@@ -0,0 +1,3 @@
+def average(values):
+    """Return the average; return 0.0 when values is empty."""
+    return sum(values) / len(values)
```

`average([])` 会触发除零，与文档约定的 `0.0` 返回值冲突。打开任务详情，可以沿着候选发现、Critic 复核、门禁检查和最终结论查看处理过程。

将最后一行改为下面的实现，再提交一轮审查，对照两次报告：

```python
return sum(values) / len(values) if values else 0.0
```

<p align="center">
  <a href="docs/review.png"><img src="docs/review.png" width="100%" alt="EvoAgent 审查报告：代码位置、除零证据、修复建议与 GitHub 评论状态"></a>
</p>
<p align="center"><sub>任务详情集中展示风险等级、代码证据、修复建议、模型用量与评论发送状态。</sub></p>

| 在报告中查看 | 内容 |
| --- | --- |
| **审查结论** | 文件与行号、问题说明、代码证据、修复和测试建议 |
| **执行过程** | 时间线、角色协作、模型与工具调用、上下文和 Token 用量 |
| **原始 JSON** | 一键复制或下载完整结构化报告 |

<p align="right"><a href="#top">↑ 返回顶部</a></p>

<a name="github"></a>

## 🔗 接入 GitHub

通过 **仓库 Webhook + Cloudflare Tunnel + PAT**，将 PR 更新接入审查工作流。

### 1 · 配置 PAT 与 Webhook Secret

为目标仓库创建 fine-grained PAT，按功能授予权限：

| 功能 | 仓库权限 |
| --- | --- |
| 读取 PR 与 Diff | `Contents: Read`、`Pull requests: Read` |
| 回写审查评论 | `Pull requests: Read and write` |
| 创建修复分支与 Draft PR | `Contents: Read and write`、`Pull requests: Read and write` |

另生成一份随机密钥作为 Webhook Secret：

```bash
python -c 'import secrets; print(secrets.token_urlsafe(48))'
```

填写 `.env`，随后重启 EvoAgent：

```dotenv
EVOAGENT_GITHUB_WEBHOOK_SECRET=your_webhook_secret
EVOAGENT_GITHUB_TOKEN=your_repository_pat
EVOAGENT_AUTO_POST_REVIEW=true
```

### 2 · 启动 Cloudflare Tunnel

安装 [cloudflared](https://developers.cloudflare.com/tunnel/get-started/quick-tunnels/) 后，在另一个终端运行：

```bash
cloudflared tunnel --url http://127.0.0.1:8080
```

保持 EvoAgent 和 Tunnel 运行，将命令输出的 HTTPS 地址用于下一步。Quick Tunnel 重启后，按新地址更新 Webhook 的 Payload URL。

### 3 · 添加仓库 Webhook

进入 GitHub 仓库的 **Settings → Webhooks → Add webhook**：

| 配置项 | 填写内容 |
| --- | --- |
| Payload URL | `https://<Tunnel域名>/webhooks/github` |
| Content type | `application/json` |
| Secret | 与 `.env` 中的 Webhook Secret 一致 |
| Events | 选择 **Pull requests** |
| SSL / Active | 开启 SSL 验证并保持 Active |

新建、重开 PR 或推送新提交后，Recent Deliveries 返回 `202`，工作台出现异步审查任务。审查完成后，报告会回写到 PR 评论区。

每份报告记录 reviewed SHA，并在发布评论前核对 PR 版本。任务详情可查看评论发送状态，或重试失败的评论发布。权限说明见 [GitHub 评论 API 文档](https://docs.github.com/en/rest/issues/comments)。

<a name="evolution"></a>

## 🧠 反馈与演进

在任务详情中记录 **误报、漏报或修复问题**。反馈沉淀为失败案例与审查记忆，为后续检索和候选改进提供依据；标注评测集用于新旧版本回放。

```text
审查报告 → 人工反馈 → 记忆与改进信号 → Prompt / Skill 候选
                                          ↓
                              validation / holdout 回放
                                          ↓
                                  Smoke → 激活版本
                                          ↓
                                    用于后续任务
```

### 准备评测案例

导入内置评测案例：

```bash
python -B scripts/seed_evaluation_cases.py --builtin --db-path evoagent.db
```

也可通过 `POST /v1/evaluation/cases` 添加自己的标注案例。上面的命令显式指定 SQLite 数据库路径，使用其他路径时同步调整。

### 迭代 Prompt 与 Skills

- **Prompt**：在演进实验室中使用 `llm-review` 版本组发起演进，查看候选、评测结果与版本记录。
- **Agent Skill**：`skills/` 中的 `SKILL.md` 定义领域步骤与允许工具，Lead 按任务选用；候选版本通过 `/v1/skill-evolution/*` API 管理。
- **激活与回滚**：候选达到提升门槛，并通过验证集、独立 holdout 和 smoke 检查后默认自动激活，影响后续任务；可回滚到先前版本。
- **规则更新**：编辑磁盘 Skills 后，在能力库重新加载，或调用 `POST /v1/skills/reload`。

<p align="right"><a href="#top">↑ 返回顶部</a></p>

<a name="development"></a>

## 🛠️ 开发与配置

<details>
<summary><strong>⚙️ 运行参数与账户配置</strong></summary>

`.env` 在启动时加载，进程环境变量优先。主要参数如下：

| 配置 | 默认值 | 作用 |
| --- | ---: | --- |
| `EVOAGENT_TOTAL_TOKEN_BUDGET` | 32000 | 整项任务的输入与输出 Token 总池 |
| `EVOAGENT_AGENT_TOKEN_BUDGET` | 8000 | 每角色累计额度，跨多轮、重试和恢复共享 |
| `EVOAGENT_TIMEOUT_SECONDS` | 120 | 本次任务执行时间上限；快速开始示例设为 240 |
| `EVOAGENT_AGENT_TIME_BUDGET_SECONDS` | 60 | 单次角色执行时间上限 |
| `EVOAGENT_MAX_STEPS` | 8 | Runtime 节点执行步数上限 |
| `EVOAGENT_ASYNC_WORKERS` | 2 | 服务实例内异步并发数 |

预算充足时发送完整 Diff，超限时按完整代码行摘录并保留原始位置。Lead 分派、综合与后续重试累计使用同一角色额度。

`EVOAGENT_BOOTSTRAP_ADMIN_USERNAME` 与 `EVOAGENT_BOOTSTRAP_ADMIN_PASSWORD` 用于首次初始化账户，已有账户保留原密码。数据库路径通过 `EVOAGENT_DB_PATH` 配置。

</details>

<details>
<summary><strong>📋 报告状态与常用 API</strong></summary>

任务执行、结论复核和评论发布分别记录：

| 字段 | 含义 |
| --- | --- |
| `state` | 任务执行状态，例如 `SUCCESS`、`FAILED`、`CANCELLED` |
| `execution.review_status` | `complete` 表示复核流程完成，`needs_review` 表示有候选待人工复核 |
| `execution.finding_counts` | 原始候选、Lead 接纳、门禁通过和最终发布数量 |
| `publication_status` | 评论发布状态，例如 `pending`、`sent`、`failed`、`superseded` |

常用接口使用登录取得的 Bearer Token：

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| `POST` | `/v1/auth/login` | 登录并获取 `access_token` |
| `POST` | `/v1/reviews` | 同步审查；加 `?async=true` 使用异步队列 |
| `GET` | `/v1/tasks/{id}` | 任务状态、执行记录与报告 |
| `GET` | `/v1/tasks/{id}/report` | 下载 Markdown 报告 |
| `POST` | `/v1/tasks/{id}/resume` | 恢复任务或重试评论发布 |
| `POST` | `/v1/tasks/{id}/cancel` | 取消任务 |
| `POST` | `/v1/tasks/{id}/feedback` | 提交反馈 |
| `POST` | `/v1/tasks/{id}/fix` | 验证修复并创建 Draft PR |
| `GET/POST` | `/v1/evaluation/cases` | 查询与添加标注案例 |
| `GET` | `/v1/evolution/status` | 查询 Prompt 演进与评测集状态 |
| `GET` | `/v1/skill-evolution/status` | 查询 Agent Skill 演进状态 |
| `GET` | `/metrics` | Prometheus 指标 |

审查完成后可独立重试评论发布，恢复时复用已完成的审查结果。队列死信记录通过 `GET /api/queue/dead-letters` 查询。

</details>

<details>
<summary><strong>🐳 Docker · PostgreSQL · Redis</strong></summary>

根目录 Compose 启动 EvoAgent、PostgreSQL 和 Redis。在 `.env` 中配置好模型连接、登录签名密钥和管理员密码后，运行：

```bash
docker compose up -d --build
docker compose ps
curl -fsS http://127.0.0.1:8080/health
```

工作台地址仍为 **http://127.0.0.1:8080/**。常用管理命令：

```bash
docker compose logs -f evoagent
docker compose exec evoagent python scripts/seed_evaluation_cases.py --builtin
docker compose down
```

Compose 为 PostgreSQL 和 Redis 配置持久卷。使用自备后端时，设置 `EVOAGENT_DATABASE_URL` 与 `EVOAGENT_REDIS_URL`；运行单个 EvoAgent 服务实例，通过 `EVOAGENT_ASYNC_WORKERS` 调整实例内并发。

Redis Streams 提供 ACK、执行中续租、断连重试与死信记录。容器内的案例导入命令从进程环境读取 PostgreSQL 连接。

</details>

<details>
<summary><strong>🛠️ 修复验证与 Draft PR</strong></summary>

为 PAT 授予 `Contents` 和 `Pull requests` 读写权限，并配置 PR 源仓库与测试命令：

```dotenv
EVOAGENT_TRUSTED_GITHUB_REPOSITORIES=owner/repository
EVOAGENT_REPAIR_TEST_COMMAND=python -m unittest discover -s tests
```

白名单填写 PR head 所在的源仓库。在 GitHub 审查任务详情中点击创建修复分支，系统读取已审查版本、生成补丁，依次执行语法检查、原始仓库测试与修复后测试，通过验证后提交 Draft PR。

</details>

<details>
<summary><strong>🧪 测试与开发检查</strong></summary>

从项目根目录运行回归测试：

```bash
python -B scripts/run_tests.py
```

浏览器检查使用 Playwright 与 Chromium：

```bash
python -m pip install playwright
python -m playwright install chromium
python -B tests/browser_smoke.py
```

指定专用 PostgreSQL / Redis 服务运行后端检查：

```bash
python -B tests/run_backend_smoke.py \
  --redis-url redis://127.0.0.1:6379/0 \
  --database-url postgresql://user:password@127.0.0.1:5432/test_database
```

</details>

<a name="structure"></a>

## 📁 项目结构

```text
EvoAgent/
├── evoagent/              # API、Agent 编排、门禁、记忆与演进
├── web/                   # 审查工作台
├── skills/                # 领域审查 Skills
├── scripts/               # 评测、案例导入与开发脚本
├── tests/                 # 核心流程回归与按需检查
├── docs/                  # 工作台与审查结果截图
├── .env.example           # 环境配置示例
├── docker-compose.yml     # PostgreSQL / Redis 运行入口
└── LICENSE
```

## License

[MIT](LICENSE) · Copyright (c) 2026 EvoAgent contributors

<p align="center"><sub>EvoAgent · 多 Agent 代码审查与反馈演进工作台</sub><br><a href="#top">返回顶部 ↑</a></p>
