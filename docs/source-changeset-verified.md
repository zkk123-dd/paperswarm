# 源码改造清单 v2（已核实版）

**本文件取代 `phase0-1-design.md` §2.4 的原改造方案。** 原方案基于推测，经实读源码后有三处判断是错的，已在 §0 列出。所有结论均标注了核实来源。

---

## 0. 三处推翻原方案的发现

### 发现 1：不能加新 mode —— 加一个 mode 要动 11 个耦合结构

原方案写"在 `jiuwenswarm/runtime/mode_catalog.py` 新增 `paper` 模式"。**这是错的。**

实读结果：

| 文件 | 实际情况 |
|---|---|
| `runtime/mode_catalog.py` | 只管**单 Agent** 模式（`agent.work.normal` 等 4 种），文件头写明 "single-Agent Runtime mode capability catalog"。团队模式根本不在这里 |
| `common/mode_matrix.py` | 真正的模式矩阵：`{agent,team} × {work,code} × {normal,plan}` 共 **8 种 canonical 模式**。新增一种要同时改：`NEW_*` 常量、`WEB_COMPOSABLE_MODES`、`MODE_ALIASES`、`NEW_CANONICAL_MODES`、`DEPRECATION_MAP`、`TEAM_CANONICAL_MODES`、`SINGLE_AGENT_CANONICAL_MODES`、`PLAN_CANONICAL_MODES`、`_PLAN_EXIT_MODES`、`_WEB_MODE_TABLE`、`NEW_CANONICAL_MODE_RESOLUTION`——**11 个结构**，且都有测试守着 |

**结论**：**不加 mode**。用现成的 `team` 模式（集群）+ `enable_swarmflow: true`，把论文流水线的行为放进 Swarm Skill 与 SwarmFlow 脚本里。改动面从"11 个矩阵结构 + 回归风险"降到 0。

### 发现 2：新增元素是**自门控**的，不需要按 mode 分档

原方案要新增 `_PAPER_RAIL_NAMES` / `_PAPER_TOOL_NAMES` 分档。**不需要。**

`config_specs.py` 里有一句关键注释：

> Tools common to both roles. Each element self-gates on config, so all are declared; **an unconfigured element simply yields no tools.**

即：把元素声明进 `_COMMON_TOOL_NAMES` / `_COMMON_RAIL_NAMES`，由元素自己在工厂里读 `ctx.config` 决定返回对象还是 `None`。这是官方既定模式（`swarm.send_file` 就是这样按 channel 开关自门控的）。

**结论**：元素声明进现有清单 + 工厂内自门控 `config.paper.enabled`。不碰 `build_member_capability_specs` 的分档逻辑。

### 发现 3：不用写自定义 Rail 子类 —— 门控改用「流水线步骤」

原方案要写 4 个 Rail。但工厂 Rail 必须返回一个 **Rail 对象**，而 Rail 基类契约在 `openjiuwen.harness.rails`（属 agent-core 仓库，我未读到其接口定义）。

**不臆造接口。** 改成：

- **主要强制点 = SwarmFlow 脚本里的一个步骤**。脚本是我们自己写的 Python（已核实契约：顶层 `META={...}` + `async def run(args)`），门控直接在其中调用 `paperswarm.tex_gate`，失败即 `raise`，中止流水线。**这条路 100% 在已核实契约内。**
- **次要强制点 = PreToolUse 钩子**（契约已核实，见 §3），作为纵深防御。但钩子的**拦截协议**（如何向运行时表达"阻止这次工具调用"）尚未核实 → Q12。

---

## 1. 已核实的元素写法（对照样例）

`providers/runtime_tools.py` 的 `swarm.send_file` 是完整范例，逐项对照：

```python
class SendFileInput(ConstructionInput):
    channels_config: dict[str, Any] = param_field(default_factory=dict, description="...")
    request_id: str | None = context_field(attr="request_id", description="...")
    channel_id: str | None = context_field(attr="channel_id", description="...")

@harness_element(
    kind=ElementKind.TOOL,
    name=SEND_FILE,
    description="...",
    input_model=SendFileInput,
)
def build_send_file_tools(params: dict[str, Any], ctx: SwarmBuildContext) -> list[Any]:
    inp = SendFileInput.resolve(params, ctx)
    if not inp.request_id or not inp.channel_id:
        return []          # ← 自门控：跳过即返回空
    ...
```

要点（全部来自实读）：

| 契约 | 内容 |
|---|---|
| 工厂签名 | `factory(params: dict, context: BuildContext) -> obj \| list \| None` |
| 声明装饰器 | `@harness_element(kind=ElementKind.TOOL/RAIL, name=..., description=..., input_model=...)` |
| 参数来源声明 | 属性用 `param_field(...)`（由 `config_specs` 烘焙进 spec params）；环境用 `context_field(attr=... \| resolver=...)`（来自 `SwarmBuildContext`） |
| 取值方式 | `inp = XxxInput.resolve(params, ctx)`，返回 pydantic 校验过的对象 |
| 门控 | 返回 `None` / `[]` = 本次不挂载该能力 |
| 无入参元素 | 可省略 `input_model`（见 `HEARTBEAT` / `PERSONAL_CONTEXT`） |
| 注册 | 只声明 `@harness_element`；`registry.register_swarm_providers()` 经 `register_from_catalog()` 批量注册。**不需要手写注册调用** |
| 常量导出 | 在 `providers/*.py` 定义 `NAME = "swarm.xxx"`，并在 `registry.py` re-export，供 `config_specs` 按符号引用 |
| 日志 | 各 provider 用 `logger.info("[swarm.xxx] ...")` 格式，跳过分支给 `logger.info`，异常给 `logger.warning` 并返回空 |

---

## 2. 最终改造清单

### 2.1 新增文件（3 个）

| 路径 | 内容 |
|---|---|
| `jiuwenswarm/agents/harness/paper/__init__.py` | 包声明 |
| `jiuwenswarm/agents/harness/paper/store.py` | 证据台账与门控（**已实现并通过测试**，见 §4） |
| `jiuwenswarm/agents/swarm/providers/paper.py` | 声明式 provider：`@harness_element` + `ConstructionInput` + 工厂，全部自门控 |

### 2.2 修改文件（2 个）

| 路径 | 改动 | 风险 |
|---|---|---|
| `jiuwenswarm/agents/swarm/providers/__init__.py` | 引入 `paper` 模块，触发其 `@harness_element` 声明 | 极低（仅 import） |
| `jiuwenswarm/agents/swarm/registry.py` | import `paper`；re-export 元素名常量；加入 `__all__` | 低 |

**注意**：因为采用自门控（发现 2），**`config_specs.py` 也需要改**——把新元素名加进 `_COMMON_RAIL_NAMES` / `_COMMON_TOOL_NAMES`，并加对应的 `_RAIL_PARAM_BUILDERS` / `_TOOL_PARAM_BUILDERS` 条目把 `config.paper.*` 烘焙进 params。这是一个额外修改文件：

| 路径 | 改动 | 风险 |
|---|---|---|
| `jiuwenswarm/agents/swarm/config_specs.py` | 元素名加进 `_COMMON_*_NAMES`；新增 `_paper_*_params(c)` 并注册进 `_RAIL_PARAM_BUILDERS` / `_TOOL_PARAM_BUILDERS` | 中（在公共路径上，需回归 `test_swarm_assembly.py`） |

### 2.3 元素清单（7 个 Tool，0 个自定义 Rail）

| 元素名 | 职责 | param（属性） | context（环境） | 自门控条件 |
|---|---|---|---|---|
| `swarm.paper_evidence` | 台账读写：register artifact / add claim / verify | `ledger_root`, `artifacts_root`, `float_tolerance` | `team_ws_root`, `request_id` | `paper.enabled` |
| `swarm.paper_tex_gate` | 校验并回填 `\result{claim_id}` | `result_macro`, `max_pages`, `max_pdf_bytes` | `team_ws_root`, `project_dir` | `paper.enabled` |
| `swarm.paper_experiment_runner` | 经 jiuwenbox 在沙箱执行复现实验 | `sandbox_url`, `cpu_max`, `memory_max`, `pids_max`, `wall_clock_cap`, `network_mode`, `allowed_domains` | `project_dir`, `team_ws_root`, `request_id` | `paper.enabled` + `sandbox.enabled` |
| `swarm.paper_artifact_fetch` | 拉 arXiv PDF / 代码仓库 / 数据集 | `cache_root`, `max_bytes`, `allowed_hosts` | `team_ws_root`, `request_id` | `paper.enabled` |
| `swarm.paper_latex_build` | 编译 PDF 并核查页数/体积 | `engine`, `max_pages`, `max_pdf_bytes`, `template_id` | `project_dir`, `team_ws_root` | `paper.enabled` |
| `swarm.paper_resource_report` | 从轨迹聚合 token/时长 | `trace_dir`, `group_by` | `team_ws_root`, `session_id` | `paper.enabled` |
| `swarm.paper_review_submit` | 生成 paperreview.ai 提交包并回填 token（**危险操作**） | `endpoint`, `venue`, `max_pdf_bytes` | `request_id`, `channel_id`, `session_id` | `paper.enabled` + `paper.review.enabled`（且必须 HITL 确认） |

> **为什么 0 个自定义 Rail**：见发现 3。门控改由 SwarmFlow 脚本步骤承担，Rail 化留待 Rail 基类契约核实后再评估（Q11）。

### 2.4 不修改的文件（明确列出，避免误改）

| 文件 | 为什么不动 |
|---|---|
| `runtime/mode_catalog.py` | 只管单 Agent 模式，与本需求无关 |
| `common/mode_matrix.py` | 不加 mode，矩阵不动（发现 1） |
| `agents/swarm/assembly.py` | enrich 流程与元素集合无关 |
| `agents/swarm/context.py` | `SwarmBuildContext` 现有字段已够用（`team_ws_root` / `project_dir` / `request_id` / `session_id` / `channel_id` / `config` 均有） |

---

## 3. `config.yaml` 增量（契约已核实）

### 3.1 paper 配置段（自定义，本方案新增）

```yaml
paper:
  enabled: true
  ledger_root: ""              # 空 = 默认落到 team_ws_root/paper-ledger
  result_macro: "result"
  compile:
    engine: "xelatex"
    max_pages: 15              # paperreview.ai 只分析前 15 页
    max_pdf_bytes: 10485760    # 10 MiB，paperreview.ai 硬上限
  experiment:
    wall_clock_cap: 1800
    cpu_max: "2"
    memory_max: "8G"
    pids_max: 512
  review:
    enabled: false             # 对外提交默认关闭
    venue: "ICLR"
```

### 3.2 hooks 段（**契约来自仓库 config.yaml 模板原文注释**）

```yaml
hooks:
  disable_all_hooks: false
  PreToolUse:
    - matcher: "Write|Edit"
      hooks:
        - type: command
          command: "python -m paperswarm.tex_gate --root \"$PAPERSWARM_LEDGER\" check \"$ARGUMENTS\""
          timeout: 30
          shell: "bash"
          status_message: "校验 LaTeX 数值是否有台账来源..."
  Stop:
    - matcher: "*"
      hooks:
        - type: command
          command: "python -m paperswarm.evidence --root \"$PAPERSWARM_LEDGER\" verify"
          timeout: 60
          shell: "bash"
```

已核实的钩子契约：事件 `PreToolUse` / `PostToolUse` / `SessionStart` / `Stop`；条目字段 `matcher`（如 `"Write|Edit"`、`"Bash"`、`"*"`）+ `hooks[]`；单条钩子 `type: command`（配 `command` / `timeout` / `shell` / `status_message`）或 `type: prompt`（配 `prompt` / `timeout`），prompt 支持 `$ARGUMENTS`。

> **未核实**：`command` 钩子收到什么入参（stdin JSON？环境变量？`$ARGUMENTS` 是否同样可用），以及**如何表达"阻止本次工具调用"**（退出码？stdout JSON？）→ **Q12**。在 Q12 核实前，钩子只是纵深防御，**真正的强制点在 SwarmFlow 脚本步骤**。

### 3.3 sandbox 段（契约来自 `jiuwenbox/README.md`）

```yaml
sandbox:
  url: "http://127.0.0.1:8321"
  type: "jiuwenbox"
  startup_mode: "internal"
  policy_file: "code-agent-policy.yaml"
  enabled: true
```

### 3.4 team / 观测段

```yaml
modes:
  team:
    jiuwen_team:
      enable_swarmflow: true
      swarmflow_budget: 8000000        # 硬顶；留 20% 余量，触顶 run 直接 failed 且不可续跑
      swarmflow_concurrency:
        max_workflows: 2
        max_agents_total: 12
team_observability:
  enabled: true
  exporter: file
trajectory_ui:
  enabled: true
```

---

## 4. 已实现并通过验证的部分

`paper-swarm/src/paperswarm/` 下的证据层**不依赖任何 Agent 框架**，因此现在就能跑、能测——这也是它相对框架耦合代码的优势。

| 文件 | 内容 |
|---|---|
| `src/paperswarm/evidence.py` | 台账：artifact SHA-256 登记、claim 登记（校验声明值必须与产物一致）、`verify()` 四重校验 |
| `src/paperswarm/tex_gate.py` | `\result{claim_id}` 扫描、门控、数值回填、手写数字启发式告警 |
| `tests/test_evidence.py` | 9 项端到端断言 |

`verify()` 的四重校验：① claim 绑定的 artifact 已登记；② 产物文件仍存在；③ **产物 SHA-256 与登记时一致**；④ 按 JSON Pointer 从产物读出的实际值与 claim 声明值一致。

**实测结果（2026-09-29）**：

```
PASS  test_claim_requires_registered_artifact
PASS  test_claim_value_mismatch_rejected
PASS  test_gate_passes_and_injects_values
PASS  test_literal_number_warning_is_not_blocking
PASS  test_macro_redefinition_blocks_gate
PASS  test_missing_artifact_file_blocks_gate
PASS  test_register_and_verify_pass
PASS  test_tampered_artifact_blocks_gate
PASS  test_unknown_claim_id_blocks_gate

9/9 passed
```

其中 `test_tampered_artifact_blocks_gate` 就是防伪造能力的直接证据：把实验产物里的 `val_ppl` 从 16.81 改成 1.02（伪造一个漂亮结果），门控立刻失败并报出「artifact 内容已被修改（摘要不匹配，疑似篡改实验结果）」。

复现命令：

```bash
cd paper-swarm
"C:/Users/周凯/.workbuddy/binaries/python/versions/3.13.12/python.exe" tests/test_evidence.py
```

---

## 5. 沙箱选型的硬结论（Q4 已核实）

`jiuwenbox` **就是**官方沙箱，能力充分：

| 能力 | 落实方式 |
|---|---|
| 进程隔离 | `bubblewrap` |
| 文件系统 | 静态策略（`read_only` / `read_write` / `bind_mounts`）+ 内核 Landlock 强制 |
| 系统调用 | seccomp 过滤（如屏蔽 `ptrace` / `mount` / `bpf`） |
| 资源上限 | cgroup v2/v1：`memory_max` / `cpu_max` / `pids_max` |
| 网络 | netns + iptables，支持 `allowed_domains` / `blocked_domains` / `allowed_ports` |
| 审计 | 每沙箱一份 JSONL 审计日志（`exec_command` 含 `exit_code` / `stdout` / `duration_ms`）——**可直接作为资源报告的证据源** |
| 密钥保护 | Inference Privacy Proxy 支持按路径注入 API key，客户端只拿占位 key |

**但有一条硬约束：`jiuwenbox` 只支持 Linux**（README "Requirements" 首行即 `Linux`，依赖 bubblewrap / iproute2 / iptables / nftables，且需要 `NET_ADMIN`）。

当前开发机是 **Windows**。因此复现实验的执行环境必须先解决：

| 方案 | 说明 |
|---|---|
| **WSL2 + 在 WSL 内装 jiuwenswarm + jiuwenbox** | 推荐。ML 复现本来就基本只在 Linux 上跑得通（依赖 wheel、编译工具链），这一步迟早要做 |
| Docker（仓库根目录已有 `Dockerfile.claw`，`jiuwenbox/scripts` 有 `build_docker.sh` / `run_docker.sh`） | 可行，但要确认容器内的目录挂载与 `preserve_file_sharing_mode: mount` 的路径一致 |
| 远端 Linux 主机（`startup_mode: external`） | 需要 jiuwenbox 主机能看到 jiuwenswarm 的同路径文件（因为共享模式固定为 `mount`） |

**这项决定不能推迟**：没有 Linux 环境，`swarm.paper_experiment_runner` 一步都跑不了，论文的实验部分就没有数据。

---

## 6. 待确认清单（本文件新增）

| 编号 | 待核实 | 方式 |
|---|---|---|
| Q11 | `openjiuwen.harness.rails` 的 Rail 基类契约（决定是否需要把门控升级为 Rail） | 读 agent-core 源码 `openjiuwen/harness/rails/` |
| Q12 | `command` 类型钩子的入参格式，以及**如何表达拦截**（退出码 / stdout JSON） | 读 jiuwenswarm hooks 实现 + `docs/zh/工具权限与安全防护.md` |
| Q13 | `openjiuwen` Tool 对象的基类契约（`ToolCard` 之外还有哪些必须实现的成员） | 读 agent-core `openjiuwen/core/foundation/tool` |
| Q14 | 复现实验执行环境最终选 WSL2 还是 Docker | 本机实测；需确认 `bubblewrap` 在 WSL2 内核下可用 |
