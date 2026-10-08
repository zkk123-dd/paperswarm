# 上游 CI 测试运行说明（`test_paper_providers.py`）

> 目的：让 `tests/agents/swarm/test_paper_providers.py` **在真实 openjiuwen 上跑通**，而不只是通过语法编译。
> 本机为什么跑不了、上游 CI 怎么跑、跑出来应该看到什么——三件事都在这一页。

---

## 1. 结论（先说清状态）

| 环境 | 命令 | 状态 |
|---|---|---|
| 上游 CI（装好 openjiuwen 的 Linux runner） | `pytest tests/agents/swarm/test_paper_providers.py -v` | **待在上游执行**（本机无 openjiuwen） |
| 本机隔离 env（无 openjiuwen） | 同上 | ❌ 无法执行：`import openjiuwen` 失败 |
| 本机隔离 env 的**等价替身测试** | `pytest integration/test_paper_providers_contract.py -q` | ✅ **17 passed**（已实测，见 §4） |
| 本机静态检查 | `python -m py_compile tests/agents/swarm/test_paper_providers.py` | ✅ 通过 |

**这不是回避**：测试文件已写好并静态通过；缺的是框架依赖树（`openjiuwen` 及其 fastapi / playwright 等），
而这棵树属于上游 CI 的职责。本机用替身测试覆盖同一批行为契约，两者互补而非替代——替身测试只证明
"给定文档化契约，本模块行为正确"，真实装配回归仍必须由 CI 那一侧给出。

---

## 2. 被测对象与用例清单

被测文件：`jiuwenswarm/agents/swarm/providers/paper_providers.py`
测试文件：`tests/agents/swarm/test_paper_providers.py`（**18 个用例，其中 8 个 async**）

| # | 用例 | 覆盖 |
|---|---|---|
| 1 | `test_paper_elements_register_with_expected_kinds` | 4 个 `swarm.paper_*` 元素进 catalog，kind 为 TOOL/RAIL |
| 2 | `test_paper_input_fields_are_source_tagged` | 每个字段的 `source` 是 `params` 还是 `context`，context 字段必须有 `context_attr` 或 `resolver_ref` |
| 3 | `test_paper_factories_and_resolver_resolve` | `factory_ref` / `resolver_ref` 反解回可调用对象 |
| 4 | `test_paper_input_model_rejects_a_wrong_type` | input model 是真 pydantic 模型，不是宽松 dict |
| 5 | `test_paper_run_dir_resolver_prefers_project_dir` | `project_dir` 优先，`workspace.root_path` 兜底，都没有则 `None` |
| 6 | `test_paper_factories_self_gate_without_a_run_dir` | **自门控**：无 run_dir → 工厂返回 `[]`/`None`，不需要调用方判断 |
| 7 | `test_config_specs_gate_the_paper_capability` | `paper_swarm.enabled` 才追加 4 个元素；默认团队零 paper 元素（catalog ↔ registry 精确相等） |
| 8 | `test_ledger_tool_chain_writes_a_gated_section` | 台账工具链端到端：`register_artifact` → `add_claims` → `write_latex_section` 合规落盘 |
| 9 | `test_ledger_rejects_a_claim_that_disagrees_with_the_artifact` | claim 声明值与产物实际值不符 → 拒绝 |
| 10 | `test_protocol_rail_injects_the_macro_verbatim` | 协议 rail 注入的文本里 `\result{claim_id}` **原样保留**（没被当格式字段吃掉） |
| 11 | `test_gate_audit_records_a_bypass_and_leaves_the_file_alone` | 审计 rail 检测到"正文含手写数值"的越权写入 → **留痕但不改文件** |
| 12 | `test_gate_audit_passes_a_macro_only_section` | 纯宏引用的正文 → 审计放行 |
| 13 | `test_gate_audit_ignores_writes_outside_the_run_dir` | 写 run_dir 之外的文件 → 不误报 |
| 14 | `test_protocol_rail_builds_through_railspec` | 经 `RailSpec` 装配路径也能建出 rail |
| 15 | `test_assembler_reports_failure_instead_of_emitting_a_pdf` | 无模板时**结构化失败**，不产出降级 PDF |
| 16 | `test_paper_kernel_is_importable_from_its_vendored_location` | `jiuwenswarm.agents.paper` 可导入 |
| 17 | `test_paper_elements_are_inert_without_an_opted_in_team` | 未 opt-in 的团队配置下元素完全惰性 |
| 18 | `test_paper_provider_name_constants_are_re_exported` | `registry` 正确 re-export 4 个常量 |

测试**不调用 LLM、不联网、不调 tectonic**——唯一的编译相关用例断言的是失败契约。

---

## 3. 上游 CI 怎么跑

### 3.1 依赖与运行命令

```bash
# 依赖来自上游 pyproject.toml 的 [test] extra（版本号为该文件原文）
pip install -e ".[test]"
# → pytest>=8.3.5, pytest-asyncio>=1.0.0, pytest-html>=4.1.1,
#   pytest-mock>=3.14.0, pytest-cov>=7.0.0

# 只跑本次新增的测试
pytest tests/agents/swarm/test_paper_providers.py -v

# 关掉覆盖率插件可以更快（pytest.ini 的 addopts 默认带 --cov）
pytest tests/agents/swarm/test_paper_providers.py -v -p no:cacheprovider --no-cov
```

`openjiuwen` 由 `pyproject.toml` 直接 pin 在 commit 上，不需要手动处理：

```
openjiuwen @ git+https://gitcode.com/openJiuwen/agent-core.git@9e3390195a9ea15235b2b5f7412cb2aa440622cc
```

> 仓库地址核实来源：`README_CN.md` 中的 GitCode 链接；pin 的 commit 核实来源：`pyproject.toml` 原文。
> 上游仓库：`https://gitcode.com/openJiuwen/jiuwenswarm`，PR 入口 `/pulls`。

### 3.2 两个必须知道的配置约束

`pytest.ini` 里有两条会影响结果的设置，跑挂了先看这两条：

| 设置 | 值 | 后果 |
|---|---|---|
| `filterwarnings` | `error` | **任何 Warning 都会变成失败**。新用例不得触发 DeprecationWarning 之外的告警 |
| `addopts` | `--asyncio-mode=auto` | async 用例不需要 `@pytest.mark.asyncio` 装饰器 |

### 3.3 期望输出

```
tests/agents/swarm/test_paper_providers.py ......................  [100%]
18 passed
```

### 3.4 顺带要跑的回归（证明没改坏既有装配）

```bash
pytest tests/agents/swarm/test_manifest_catalog.py tests/agents/swarm/test_swarm_assembly.py -v
```

这两条对应 `source-changeset-verified.md` §3 的 `config_specs` 回归项——
本次改动落在 `config_specs.py` 这条公共路径上，必须证明既有团队装配仍全绿。

---

## 4. 本机实际跑通的替身测试（已实测证据）

```bash
# 隔离 env
PY="C:/Users/周凯/.workbuddy/binaries/python/envs/default/Scripts/python.exe"
PYTHONPATH="E:/存放/agent/paper-swarm/src" "$PY" -m pytest \
  integration/test_paper_providers_contract.py -q
```

实测结果（2026-09-29）：

```
.................                                                        [100%]
17 passed in 0.73s
```

替代的**边界必须说清**：替身把 `openjiuwen` 侧被本模块用到的那一小撮 API
（`ConstructionInput` / `param_field` / `context_field` / `harness_element` /
`ElementKind` / `get_catalog` / `factory_ref` / `LocalFunction` / `AgentCallbackContext`）
按仓内既有用法逐条镜像，契约镜像来源写在文件头注释里。它**不**声称等价于真实 openjiuwen，
因此**不能替代** §3 的 CI 结果。两者都过，才算"改对了"。

---

## 5. 排障表

| 现象 | 根因 | 处理 |
|---|---|---|
| `ModuleNotFoundError: openjiuwen` | 没装框架依赖 | `pip install -e ".[test]"` |
| 用例被 WARNING 判失败 | `filterwarnings = error` | 消除告警，不要加 `-W ignore` 掩盖 |
| async 用例被跳过 | 缺 pytest-asyncio | `pip install pytest-asyncio>=1.0.0`（`[test]` 已含） |
| `--cov` 报缺插件 | 没装 pytest-cov | 装，或用 `--no-cov` 关掉 |
| catalog parity 断言失败 | `config_specs` 与 registry 的 `swarm.*` 常量不精确相等 | 检查是否把 paper 元素误加进 `_COMMON_*_NAMES` 默认元组（**这是回归项**，只能经 `paper_swarm.enabled` 条件追加） |
| 单测在 Windows 上路径断言失败 | 测试用 `tmp_path`，不应出现平台差异 | 若出现，说明被测代码里硬编了分隔符 |

---

## 6. 交付这份说明的意义

上游 CI 的执行结果是"框架贡献"那条验收线的一部分（见 `upstream-contribution.md` §验收）。
本机拿不到那条线时，**不假装拿到了**——所以这里明确区分了：

- ✅ 已实测：替身契约测试 17 passed、静态编译通过、`config_specs` 语法与门控正确
- ⏳ 待执行：上游 CI 的 18 个用例

在 CI 结果回来之前，"改动正确"这个结论的强度是**中**，不是**高**。
