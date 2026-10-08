# 框架贡献说明（针对 `openJiuwen/jiuwenswarm`）

> 本文说明**改了上游什么、为什么这么改、怎么证明没改坏**，以及**PR 怎么提**。
> 所有"已核实"结论都指向仓内可复读的文件；没有核实的一律标 `[待核实]`。

---

## 1. 结论

本次对上游的改动**只动 2 个既有文件 + 新增 21 个文件**，全部走官方声明式扩展点
（`@harness_element` + `config_specs` 烘焙 + `registry` re-export），没有 patch 任何私有函数。
**未 opt-in 的团队拿到与改动前完全一致的元素集合**——这是"最小侵入"的硬指标，且有回归测试守着。

---

## 2. 上游基线（已核实）

| 项 | 值 | 来源 |
|---|---|---|
| 仓库 | `https://gitcode.com/openJiuwen/jiuwenswarm`（PR 入口 `/pulls`） | `README_CN.md` |
| 底座框架 | `openjiuwen` ← `gitcode.com/openJiuwen/agent-core.git`，pin 在 commit `9e3390195a9ea15235b2b5f7412cb2aa440622cc` | `pyproject.toml` |
| Python | `>=3.11,<3.14` | `pyproject.toml` |
| License | Apache-2.0 | `pyproject.toml` |
| 测试依赖 | `pip install -e ".[test]"` → pytest ≥8.3.5 / pytest-asyncio ≥1.0.0 / pytest-cov ≥7.0.0 等 | `pyproject.toml` `[test]` extra |
| 测试配置 | `pytest.ini`：`testpaths=tests`、`--asyncio-mode=auto`、**`filterwarnings=error`** | `pytest.ini` |

> `filterwarnings = error` 这一条要特别注意：**任何告警都会变成失败**。

---

## 3. 改动清单

### 3.1 修改文件（2 个）——diff 见 `reports/upstream_changes.diff`

**① `jiuwenswarm/agents/swarm/registry.py`**

| 位置 | 改动 |
|---|---|
| providers 导入块 | `+ paper_providers as _paper_providers,` |
| 常量区 | `+ PAPER_LEDGER_TOOLS / PAPER_ASSEMBLER_TOOLS / PAPER_PROTOCOL_PROMPT / PAPER_GATE_AUDIT`（各带一段注释说明"仅声明、由 config 决定是否装配"） |
| `__all__` | `+` 上述 4 个名字 |

**② `jiuwenswarm/agents/swarm/config_specs.py`**

| 位置 | 改动 |
|---|---|
| 新增 paper 区块（`_PAPER_RAIL_NAMES` / `_PAPER_TOOL_NAMES` / `_PAPER_DEFAULT_SECTIONS` + 6 个 helper） | `+99` 行，带注释说明**为什么不加进 `_COMMON_*_NAMES`** |
| `_RAIL_PARAM_BUILDERS` | `+ registry.PAPER_PROTOCOL_PROMPT: _paper_rail_params,` `+ registry.PAPER_GATE_AUDIT: _paper_rail_params,` |
| `_TOOL_PARAM_BUILDERS` | `+ registry.PAPER_LEDGER_TOOLS: _paper_ledger_tool_params,` `+ registry.PAPER_ASSEMBLER_TOOLS: _paper_assembler_tool_params,` |
| 新增 `_paper_capability_specs(config)` | 未 enabled 时返回 `([], [])` |
| `_build_team_capability_specs` / `_build_code_capability_specs` 末尾 | `+` 条件追加（两处各 4 行） |

**为什么必须条件追加而不是加进默认元组**：`test_manifest_catalog.py::test_every_registry_name_has_descriptor`
断言 catalog 与 registry 的 `swarm.*` 常量**精确相等**。把 paper 元素加进 `_COMMON_*_NAMES`
会同时改变"默认成员档案"和这条断言的期望集合。条件追加让默认团队的集合**一个元素都不变**。

### 3.2 新增文件（21 个，见 `reports/upstream_new_files.txt`）

| 分组 | 文件 | 说明 |
|---|---|---|
| 内核（9） | `jiuwenswarm/agents/paper/{__init__,agentloop,assemble,evidence,lab,llm,telemetry,tex_gate,tools}.py` | 证据台账 + 门控 + 组装 + 有界循环。**零框架依赖**，可单独测试 |
| Provider（1） | `jiuwenswarm/agents/swarm/providers/paper_providers.py` | 4 个 `@harness_element` 声明 + 工厂，全部自门控 |
| Swarm Skill（10） | `resources/agent/workspace/skills/paper-repro-swarm/{SKILL.md,workflow.md,bind.md,dependencies.yaml,roles/*.md(5),scripts/workflow.py}` | 5 角色 × 5 小节 + mermaid + 预算约束 + Swarmflow 脚本 |
| 测试（1） | `tests/agents/swarm/test_paper_providers.py` | 18 个用例（CI 级，需 openjiuwen） |

---

## 4. 4 个 harness 元素的契约

| 元素名 | kind | 自门控条件 | 职责 |
|---|---|---|---|
| `swarm.paper_ledger_tools` | TOOL | 解析不到 run_dir 或内核导入失败 → `[]` | 暴露台账工具：完整 7 个（`register_artifact` / `add_claim` / `add_claims` / `list_artifacts` / `list_claims` / `verify_ledger` / `write_latex_section`）；`compact=True` 时收成 4 个 |
| `swarm.paper_assembler_tools` | TOOL | 同上 | 从台账回填 `\result{}`、套 ICLR 模板、调 tectonic 编译并校验页数/体积 |
| `swarm.paper_protocol_prompt` | RAIL | 解析不到 run_dir → `None` | 往系统提示里注入证据协议（**`\result{claim_id}` 原样保留**） |
| `swarm.paper_gate_audit` | RAIL | 解析不到 run_dir → `None` | **只读**事后审计：发现绕过台账直接写 `.tex` 且正文含手写数值 → 留痕，**不改文件** |

**门控的强制点在工具边界，不在 prompt。** `write_latex_section` 在下列任一情况下抛
`GateRejected` 且**不落盘**：正文零个 `\result{}` 引用 / 结果语境出现手写数字 / 引用不存在的
claim_id / 引用的 claim 校验失败（含产物被篡改）/ 正文里重定义了 `\result` 宏。
rail 只做"提示注入 + 事后只读审计"——**刻意不臆造 hook 的拒绝语义**（该契约未核实）。

---

## 5. 怎么证明没改坏（回归证据）

| 证据 | 命令 | 实测结果 | 文件 |
|---|---|---|---|
| 上游改动比对 | `python scripts/_diff_upstream_changes.py` | 2 modified + 21 new，**无声明外内容漂移** | `reports/upstream_audit.txt` |
| vendor 内核静态+功能校验 | `python scripts/verify_vendored_kernel.py` | **14/14 passed**，含"篡改产物后台账必须报错" | `reports/vendor_verify.json` |
| 上游 CI 级契约测试 | `pytest tests/agents/swarm/test_paper_providers.py` | ⏳ 待上游执行（本机无 openjiuwen） | — |
| 本机等价替身测试 | `pytest integration/test_paper_providers_contract.py` | **17 passed** | `reports/pytest_contract.txt` |
| Swarmflow 脚本 dry-run | `python scripts/verify_swarmflow_dryrun.py` | **16/16 checks** | `reports/swarmflow_dryrun.json` |
| Swarm Skill 规范校验 | `validate_swarmskill.py <pkg>` | **PASS 0 warning 0 error** | `reports/swarmskill_validate.txt` |
| 内核离线回归 | `python tests/test_evidence.py` + `test_kernel_offline.py` | **89/89 passed** | `reports/offline_regression.txt` |

**关于"内容漂移"的判据**：比对脚本对**双边做 CRLF→LF 归一**后再 diff。第一版没归一，
把上游 `agent-creator/plugin-creator` 的 SKILL.md 误报为漂移——那是 Windows 换行差异，
不是内容改动。归一后确认：**除本节列出的 2 个文件外，其余 2718 个文本文件内容零漂移。**

---

## 6. PR 计划

### 6.1 拆 2 个 PR，不打包成 1 个巨型 PR

| PR | 内容 | 合入难度 | 理由 |
|---|---|---|---|
| **PR-1**（通用性高） | `swarm.paper_gate_audit` + `swarm.paper_assembler_tools` 的**审计与编译门控抽象** | 易 | "长链路任务产物审计"与论文场景解耦，其他流水线也能用 |
| **PR-2**（场景性） | `swarm.paper_ledger_tools` + `swarm.paper_protocol_prompt` + `jiuwenswarm/agents/paper/` 内核 | 中 | 涉及新的 `paper_swarm` 配置段，需先开 issue 对齐接口形态 |

### 6.2 提交步骤（GitCode 流程）

```bash
# 1. 准备：clone 上游（默认分支已核实为 develop，不是 main）
git clone --depth 1 --branch develop \
    https://gitcode.com/openJiuwen/jiuwenswarm.git
cd jiuwenswarm
git checkout -b feat/paper-evidence-gates

# 2. 应用改动（改动清单见 §3；工作区版本即最终态）
#    核对：只应有 registry.py + config_specs.py 两个 modified
git status --porcelain

# 3. 分层提交
git add jiuwenswarm/agents/swarm/registry.py \
        jiuwenswarm/agents/swarm/config_specs.py \
        jiuwenswarm/agents/swarm/providers/paper_providers.py \
        jiuwenswarm/agents/paper \
        tests/agents/swarm/test_paper_providers.py
git commit -m "feat(swarm): evidence-gated paper capability (opt-in, self-gating elements)"

git add jiuwenswarm/resources/agent/workspace/skills/paper-repro-swarm
git commit -m "feat(skills): add paper-repro-swarm Swarm Skill (5 roles + swarmflow workflow)"

# 4. 推送并开 PR
git push -u origin feat/paper-evidence-gates
# 然后在 https://gitcode.com/openJiuwen/jiuwenswarm/pulls 开 PR
```

> **P1 已核实（2026-10-08）**：`git ls-remote --symref` 返回
> `ref: refs/heads/develop  HEAD` —— **上游默认分支是 `develop`**（`f0a69728`），
> `main`（`ce25a7b6`）不是开发主干。**PR 必须基于 `develop`**。
> 本文早前版本写的 `origin/main` 是错的，已改。
>
> **P2 已核实（2026-10-08）**：GitCode 走 **GitLab 风格 API**
> （`https://api.gitcode.com/api/v5/repos/{owner}/{repo}/...`，`PRIVATE-TOKEN` 头认证）；
> 公开端的 `branches` 端点免认证可读。`https://gitcode.com` 主站在本机被 SSL 拦，
> 但 **git 协议可达**（`git ls-remote` 成功）—— 用 `http.sslBackend=schannel`。
> 所以 **`git push` 到 fork 后开 PR 这条路是通的**，不需要 CLI。

### 6.3 PR 描述模板

```markdown
## What

Adds an opt-in, evidence-gated paper-generation capability to the swarm team profile.

## Why

Multi-agent paper pipelines can report numbers no experiment produced. Prompt-level
instructions ("do not fabricate") are unverifiable constraints. This change moves the
constraint to the tool boundary: a section that reports a result number without a
registered claim is refused and not written to disk.

## Changes

- `registry.py`: re-export 4 new element names (+1 import).
- `config_specs.py`: add the `paper_swarm` section builders; append the paper specs
  **only when `paper_swarm.enabled` is true**.
- New: `agents/paper/` kernel (zero framework deps), `providers/paper_providers.py`,
  `resources/.../skills/paper-repro-swarm/`, `tests/agents/swarm/test_paper_providers.py`.

## Compatibility

A team that does not set `paper_swarm.enabled` builds **exactly** its previous element
set. The paper elements are deliberately NOT added to `_COMMON_RAIL_NAMES` /
`_COMMON_TOOL_NAMES`, because `test_manifest_catalog.py` asserts those as exact sets.

## Tests

- `pytest tests/agents/swarm/test_paper_providers.py -v` → 18 passed
- `pytest tests/agents/swarm/test_manifest_catalog.py tests/agents/swarm/test_swarm_assembly.py -v` → unchanged
```

---

## 7. 已知不足（不掩饰）

1. **上游 CI 未执行**：18 个用例在本机拿不到真实 openjiuwen，只能静态编译 + 替身测试。
   这一点写在 `upstream-ci-runbook.md`，**不假装已经跑过**。
2. **rail 的拒绝语义未核实**：门控强制点放在工具边界（已核实契约内），rail 只做只读审计。
   等 `openjiuwen.harness.rails` 的契约可读后，再评估是否把门控升级为 rail 拦截。
3. **`git` 基线不完整**：本机上游目录**不是 git 仓库**，所以 §5 的"零漂移"是
   与 `jiuwenswarm.tar.gz` 原始归档比对得出的，不是 `git diff`。这个差别必须说清，
   因为两者证明的强度不同（归档比对能证明内容一致，不能证明 base commit）。
4. **未提交 PR**：需要 GitCode 账号与仓库写权限，本机暂缺。

---

## 8. 待确认清单

| 编号 | 内容 | 状态 |
|---|---|---|
| P1 | 上游默认分支名 | ✅ **已核实 = `develop`**（`f0a69728`，2026-10-08） |
| P2 | GitCode 的 PR 提交方式（push fork / CLI / 网页） | ✅ **已核实**：GitLab 风格 API v5；git 协议可达，push fork 后开 PR |
| P3 | `openjiuwen.harness.rails` 的 Rail 基类契约 | ⏳ 待读 agent-core 源码 |
| P4 | `command` 类型 hook 的入参格式与"如何表达拦截" | ⏳ 待读 jiuwenswarm hooks 实现 |
