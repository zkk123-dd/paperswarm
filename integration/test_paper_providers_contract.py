# Copyright (c) 2026 PaperSwarm contributors.
# SPDX-License-Identifier: Apache-2.0
"""``swarm.paper_*`` 四个 harness 元素的契约测试（无需安装 openjiuwen）。

为什么用测试替身
----------------
赛题的硬要求是"修改 JiuwenSwarm 源码"，而改动是否正确可以用两种方式证明：

* **CI 级**：安装 JiuwenSwarm + openjiuwen 整棵依赖树，跑
  ``tests/agents/swarm/test_paper_providers.py``（本仓库已提供该文件）；
* **本地级**（本文件）：把 openjiuwen 侧被本模块用到的**那一小撮** API
  做成忠实替身，直接加载 ``paper_providers`` 并做行为验证。

替身严格按仓内既有用法镜像契约（逐条对照来源写在下方的注释里），而不是按想象实现。
它**不**声称等价于真实 openjiuwen——它只证明"给定文档化的契约，本模块的行为正确"。
真实框架下的装配回归由 CI 级测试与 ``tests/agents/swarm/test_swarm_assembly.py`` 覆盖。

契约镜像来源
------------
- ``ConstructionInput`` / ``param_field`` / ``context_field`` / ``harness_element`` /
  ``ElementKind`` / ``get_catalog`` / ``factory_ref``：``swarm/DESIGN.md`` §5、§9、§13。
- 工具实例形状 ``ToolCard(name, description, input_params)`` + ``LocalFunction(card, func)``：
  ``jiuwenswarm/agents/harness/common/tools/send_file_to_user.py::SendFileToolkit.get_tools``。
- 工具入参约定（框架以关键字参数调用，参数名对齐 schema properties）：
  同文件 ``send_file(self, abs_file_path_list, target_channels=None, **_ignored)``。
- prompt rail 形状（``DeepAgentRail`` + ``PromptSection`` + ``system_prompt_builder.add_section``）：
  ``jiuwenswarm/agents/harness/team/rails/team_workspace_report_path_rail.py``。
- ``AgentCallbackContext`` / ``ToolCallInputs(tool_name, tool_args)``：
  ``tests/agents/swarm/test_swarm_assembly.py`` 中的真实用例构造。
"""

from __future__ import annotations

import asyncio
import importlib
import json
import sys
import types
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

import pytest

UPSTREAM_ROOT = Path(r"E:\存放\agent\upstream\jiuwenswarm-develop")
SWARM_PKG = UPSTREAM_ROOT / "jiuwenswarm" / "agents" / "swarm"

PAPER_PROVIDERS_MODULE = "jiuwenswarm.agents.swarm.providers.paper_providers"

_MISSING = object()


# ===========================================================================
# openjiuwen 测试替身
# ===========================================================================


def _factory_ref(target: Any) -> str:
    """复刻 ``factory_ref``：``module:qualname`` 的可反射点路径。"""
    return f"{target.__module__}:{target.__qualname__}"


class ElementKind(str, Enum):
    """元素类别（``DESIGN.md`` §9）。"""

    TOOL = "tool"
    RAIL = "rail"
    SUBAGENT = "subagent"


class _FieldSpec:
    """单个构造输入字段的声明（``param_field`` / ``context_field`` 的产物）。"""

    def __init__(
        self,
        *,
        source: str,
        default: Any = _MISSING,
        default_factory: Any = None,
        attr: str | None = None,
        resolver: Any = None,
        description: str = "",
        annotation: Any = None,
    ) -> None:
        self.source = source
        self.default = default
        self.default_factory = default_factory
        self.attr = attr
        self.resolver = resolver
        self.description = description
        self.annotation = annotation

    def default_value(self) -> Any:
        """返回字段缺省值。"""
        if self.default_factory is not None:
            return self.default_factory()
        if self.default is _MISSING:
            return None
        return self.default

    def schema_fragment(self) -> dict[str, Any]:
        """复刻真实实现写进 ``json_schema_extra`` 的 source 标记。"""
        fragment: dict[str, Any] = {"source": self.source}
        if self.source == "context":
            if self.attr is not None:
                fragment["context_attr"] = self.attr
            if self.resolver is not None:
                fragment["resolver_ref"] = _factory_ref(self.resolver)
        return fragment


def param_field(
    *,
    default: Any = _MISSING,
    default_factory: Any = None,
    description: str = "",
) -> _FieldSpec:
    """声明一个属性来源（config 烘焙）的字段。"""
    return _FieldSpec(
        source="params",
        default=default,
        default_factory=default_factory,
        description=description,
    )


def context_field(
    *,
    attr: str | None = None,
    resolver: Any = None,
    default: Any = None,
    description: str = "",
) -> _FieldSpec:
    """声明一个环境来源（运行时注入）的字段。"""
    return _FieldSpec(
        source="context",
        attr=attr,
        resolver=resolver,
        default=default,
        description=description,
    )


class ConstructionInput:
    """``ConstructionInput`` 替身：按 source 从 params / context 取值。"""

    def __init__(self, **values: Any) -> None:
        for name, spec in self._fields().items():
            object.__setattr__(self, name, values.get(name, spec.default_value()))

    @classmethod
    def _fields(cls) -> dict[str, _FieldSpec]:
        """收集本类（含基类）声明的字段，保持定义顺序。"""
        collected: dict[str, _FieldSpec] = {}
        for klass in reversed(cls.__mro__):
            for name, value in vars(klass).items():
                if isinstance(value, _FieldSpec):
                    collected[name] = value
        return collected

    @property
    def model_fields(self) -> dict[str, _FieldSpec]:
        """镜像 pydantic 的 ``model_fields``（测试里用来断言字段集合）。"""
        return self._fields()

    @classmethod
    def resolve(cls, params: dict[str, Any] | None, context: Any) -> "ConstructionInput":
        """按字段声明的来源解析取值；``None`` 结果回落到字段缺省。"""
        source_params = params or {}
        values: dict[str, Any] = {}
        for name, spec in cls._fields().items():
            if spec.source == "params":
                raw = source_params.get(name)
                value = spec.default_value() if raw is None else raw
            elif spec.resolver is not None:
                value = spec.resolver(context)
                if value is None:
                    value = spec.default_value()
            elif spec.attr is not None:
                value = getattr(context, spec.attr, None)
                if value is None:
                    value = spec.default_value()
            else:
                value = spec.default_value()
            values[name] = value
        return cls(**values)

    @classmethod
    def model_json_schema(cls) -> dict[str, Any]:
        """输出带 source 标记的 JSON Schema。"""
        properties: dict[str, Any] = {}
        for name, spec in cls._fields().items():
            properties[name] = {
                "title": name,
                "description": spec.description,
                **spec.schema_fragment(),
            }
        return {
            "title": cls.__name__,
            "type": "object",
            "properties": properties,
            "required": [],
        }

    @classmethod
    def model_validate(cls, data: dict[str, Any]) -> "ConstructionInput":
        """仅接受已声明字段（近似 pydantic 的严格校验）。"""
        unknown = set(data) - set(cls._fields())
        if unknown:
            raise ValueError(f"unexpected fields: {sorted(unknown)}")
        return cls(**data)


_CATALOG: dict[str, dict[str, Any]] = {}


def harness_element(
    *,
    kind: ElementKind,
    name: str,
    description: str,
    input_model: type[ConstructionInput] | None = None,
    builder: Any = None,
):
    """声明一个 harness 元素——纯元数据记录器，不做注册（``DESIGN.md`` §9.2）。"""

    def decorator(target: Any) -> Any:
        _CATALOG[name] = {
            "kind": kind,
            "name": name,
            "description": description,
            "factory_ref": _factory_ref(builder if builder is not None else target),
            "input_model_ref": (
                _factory_ref(input_model) if input_model is not None else None
            ),
            "input_schema": (
                input_model.model_json_schema()
                if input_model is not None
                else {"type": "object", "properties": {}, "required": []}
            ),
        }
        return target

    return decorator


def get_catalog() -> dict[str, dict[str, Any]]:
    """返回进程内 catalog。"""
    return _CATALOG


def list_elements() -> list[dict[str, Any]]:
    """返回全量 descriptor 列表。"""
    return list(_CATALOG.values())


def resolve_factory(ref: str) -> Any:
    """按 ``module:qualname`` 反射还原可调用对象（``DESIGN.md`` §9.3）。"""
    module_name, _, qualname = ref.partition(":")
    module = importlib.import_module(module_name)
    target: Any = module
    for part in qualname.split("."):
        target = getattr(target, part)
    return target


def register_from_catalog() -> None:
    """注册驱动器替身（本测试不校验 openjiuwen 注册表）。"""
    return None


@dataclass
class PromptSection:
    """``openjiuwen.harness.prompts.PromptSection`` 替身。"""

    name: str
    content: dict[str, str]
    priority: int = 50


class DeepAgentRail:
    """``openjiuwen.harness.rails.base.DeepAgentRail`` 替身。"""

    priority = 0

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        _ = (args, kwargs)

    def init(self, agent: Any) -> None:
        _ = agent

    def uninit(self, agent: Any) -> None:
        _ = agent

    async def before_model_call(self, ctx: Any) -> None:
        _ = ctx

    async def after_tool_call(self, ctx: Any) -> None:
        _ = ctx


@dataclass
class AgentCallbackContext:
    """``AgentCallbackContext`` 替身。"""

    agent: Any = None
    inputs: Any = None
    session: Any = None
    extra: Any = None


@dataclass
class ToolCallInputs:
    """``ToolCallInputs`` 替身（字段名取自真实用例的构造）。"""

    tool_name: str = ""
    tool_args: dict[str, Any] | None = None
    tool_result: Any = None
    tool_call: Any = None
    tool_call_id: str = ""


@dataclass
class ToolCard:
    """``openjiuwen.core.foundation.tool.ToolCard`` 替身。"""

    name: str
    description: str
    input_params: dict[str, Any]


@dataclass
class LocalFunction:
    """``openjiuwen.core.foundation.tool.LocalFunction`` 替身。"""

    card: ToolCard
    func: Any


@dataclass
class BuildContext:
    """``openjiuwen.agent_teams.schema.build_context.BuildContext`` 替身。

    只声明 ``SwarmBuildContext`` 会读到的逐成员字段；``derive`` 复刻"按成员派生视图"。
    """

    member_name: str = ""
    role: str = ""
    language: str = "cn"
    member_card_id: str | None = None
    workspace: Any = None
    extras: dict[str, Any] = field(default_factory=dict)

    def derive(self, **overrides: Any) -> "BuildContext":
        """派生一个新的逐成员视图。"""
        values = {
            "member_name": self.member_name,
            "role": self.role,
            "language": self.language,
            "member_card_id": self.member_card_id,
            "workspace": self.workspace,
        }
        values.update(overrides)
        derived = BuildContext(**values)
        derived.extras = dict(self.extras)
        return derived


def install_openjiuwen_double() -> None:
    """把替身模块注册进 ``sys.modules``（在导入被测模块之前调用）。"""

    def module(name: str) -> types.ModuleType:
        mod = types.ModuleType(name)
        sys.modules[name] = mod
        return mod

    manifest = module("openjiuwen.agent_teams.harness.manifest")
    manifest.ConstructionInput = ConstructionInput
    manifest.param_field = param_field
    manifest.context_field = context_field
    manifest.ElementKind = ElementKind
    manifest.harness_element = harness_element
    manifest.get_catalog = get_catalog
    manifest.list_elements = list_elements
    manifest.resolve_factory = resolve_factory
    manifest.factory_ref = _factory_ref
    manifest.register_from_catalog = register_from_catalog

    module("openjiuwen")
    module("openjiuwen.agent_teams")
    module("openjiuwen.agent_teams.harness")

    ojw_paths = module("openjiuwen.agent_teams.paths")
    ojw_paths.SKILL_VISIBILITY_FILENAME = "skills-visibility.json"
    ojw_paths.member_skill_visibility_path = (
        lambda team_id, member: Path(team_id) / "workspaces" / member
    )

    schema = module("openjiuwen.agent_teams.schema")
    schema.BuildContext = BuildContext
    build_context = module("openjiuwen.agent_teams.schema.build_context")
    build_context.BuildContext = BuildContext

    tool_mod = module("openjiuwen.core.foundation.tool")
    tool_mod.ToolCard = ToolCard
    tool_mod.LocalFunction = LocalFunction
    tool_mod.Tool = LocalFunction

    rail_base = module("openjiuwen.core.single_agent.rail.base")
    rail_base.AgentCallbackContext = AgentCallbackContext
    rail_base.ToolCallInputs = ToolCallInputs
    rail_base.AgentRail = DeepAgentRail

    module("openjiuwen.core")
    module("openjiuwen.core.single_agent")
    module("openjiuwen.core.single_agent.rail")

    prompts = module("openjiuwen.harness.prompts")
    prompts.PromptSection = PromptSection

    rails_base = module("openjiuwen.harness.rails.base")
    rails_base.DeepAgentRail = DeepAgentRail
    module("openjiuwen.harness.rails")
    module("openjiuwen.harness")


def install_swarm_shells() -> None:
    """把 ``jiuwenswarm`` / ``.agents`` / ``.agents.swarm`` 装成只带 __path__ 的壳。

    这样可以直接加载 ``...swarm.context`` 与 ``...swarm.providers.paper_providers``，
    又不执行 ``jiuwenswarm/agents/swarm/__init__.py``（它会牵出 assembly / 团队管理器等
    一大票依赖，与本测试要验证的内容无关）。
    """
    for name, path in (
        ("jiuwenswarm", UPSTREAM_ROOT / "jiuwenswarm"),
        ("jiuwenswarm.agents", UPSTREAM_ROOT / "jiuwenswarm" / "agents"),
        ("jiuwenswarm.agents.swarm", SWARM_PKG),
    ):
        shell = types.ModuleType(name)
        shell.__path__ = [str(path)]
        shell.__package__ = name
        sys.modules[name] = shell


@pytest.fixture(scope="module")
def paper_providers() -> types.ModuleType:
    """安装替身后导入被测模块（模块级缓存，导入副作用只发生一次）。"""
    install_openjiuwen_double()
    install_swarm_shells()
    return importlib.import_module(PAPER_PROVIDERS_MODULE)


def _catalog_entries() -> dict[str, dict[str, Any]]:
    """返回 catalog 里的 ``swarm.paper_*`` 条目。"""
    return {
        name: item for name, item in get_catalog().items() if name.startswith("swarm.paper")
    }


def _make_context(**overrides: Any) -> Any:
    """构造一个带项目目录的 ``SwarmBuildContext``。"""
    context_module = importlib.import_module("jiuwenswarm.agents.swarm.context")
    values = {
        "session_id": "sess-1",
        "team_id": "paper-team",
        "member_name": "writer",
        "member_card_id": "card-writer",
        "channel": "web",
        "language": "cn",
    }
    values.update(overrides)
    return context_module.SwarmBuildContext(**values)


# ===========================================================================
# 1. 声明面：名称 / 类别 / source 标注 / 反射
# ===========================================================================

EXPECTED_ELEMENTS: dict[str, str] = {
    "swarm.paper_ledger_tools": "tool",
    "swarm.paper_assembler_tools": "tool",
    "swarm.paper_protocol_prompt": "rail",
    "swarm.paper_gate_audit": "rail",
}

EXPECTED_SOURCES: dict[str, dict[str, str]] = {
    "swarm.paper_ledger_tools": {
        "run_dir": "context",
        "session_id": "context",
        "member_name": "context",
        "compact": "params",
        "sections_subdir": "params",
    },
    "swarm.paper_assembler_tools": {
        "run_dir": "context",
        "template_dir": "params",
        "tectonic_path": "params",
        "bundle_path": "params",
        "cache_dir": "params",
        "pdf_title": "params",
        "sections": "params",
        "sections_subdir": "params",
    },
    "swarm.paper_protocol_prompt": {
        "run_dir": "context",
        "language": "context",
        "sections_subdir": "params",
    },
    "swarm.paper_gate_audit": {
        "run_dir": "context",
        "sections_subdir": "params",
    },
}


def test_declares_exactly_four_paper_elements(paper_providers: types.ModuleType) -> None:
    """四个元素都被声明，且类别与常量名一一对应。"""
    constants = {
        paper_providers.PAPER_LEDGER_TOOLS,
        paper_providers.PAPER_ASSEMBLER_TOOLS,
        paper_providers.PAPER_PROTOCOL_PROMPT,
        paper_providers.PAPER_GATE_AUDIT,
    }
    assert constants == set(EXPECTED_ELEMENTS)
    for name, item in _catalog_entries().items():
        assert item["kind"].value == EXPECTED_ELEMENTS[name], name


def test_every_input_field_is_source_tagged(paper_providers: types.ModuleType) -> None:
    """每个构造输入字段都必须标注来源（属性=params / 环境=context）。"""
    entries = _catalog_entries()
    assert set(entries) == set(EXPECTED_ELEMENTS)
    for name, item in entries.items():
        properties = item["input_schema"].get("properties", {})
        assert properties, f"{name} declares no input fields"
        for prop_name, prop in properties.items():
            where = f"{name}.{prop_name}"
            assert prop.get("source") in {"params", "context"}, where
            if prop["source"] == "context":
                assert "context_attr" in prop or "resolver_ref" in prop, where


def test_param_vs_context_classification_matches_contract(
    paper_providers: types.ModuleType,
) -> None:
    """逐字段核对属性/环境分类——这是 DESIGN §6 的核心心智模型。"""
    entries = _catalog_entries()
    for name, expected in EXPECTED_SOURCES.items():
        properties = entries[name]["input_schema"]["properties"]
        assert set(properties) == set(expected), name
        for field_name, source in expected.items():
            assert properties[field_name]["source"] == source, f"{name}.{field_name}"


def test_factory_refs_and_resolvers_reflect(paper_providers: types.ModuleType) -> None:
    """``factory_ref`` / ``resolver_ref`` 都必须能反射回可调用对象且自洽。"""
    for name, item in _catalog_entries().items():
        factory = resolve_factory(item["factory_ref"])
        assert callable(factory), name
        assert _factory_ref(factory) == item["factory_ref"], name
        if item["input_model_ref"]:
            assert callable(resolve_factory(item["input_model_ref"])), name
        for prop in item["input_schema"]["properties"].values():
            ref = prop.get("resolver_ref")
            if ref is not None:
                assert callable(resolve_factory(ref)), ref


def test_descriptors_are_json_serializable(paper_providers: types.ModuleType) -> None:
    """descriptor 必须能 JSON 序列化（跨进程描述符契约）。"""
    payload = json.dumps(list_elements(), ensure_ascii=False)
    assert "swarm.paper_ledger_tools" in payload


# ===========================================================================
# 2. 自门控：配置门关着 / 目录解析不到时不产出能力
# ===========================================================================


def test_tools_self_gate_without_run_dir(paper_providers: types.ModuleType) -> None:
    """没有项目目录也没有工作区时，工具 factory 返回空列表而不是抛异常。"""
    ctx = _make_context()
    assert paper_providers.build_paper_ledger_tools({}, ctx) == []
    assert paper_providers.build_paper_assembler_tools({}, ctx) == []


def test_rails_self_gate_without_run_dir(paper_providers: types.ModuleType) -> None:
    """同样条件下，rail factory 返回 None（"该成员不参与论文任务"）。"""
    ctx = _make_context()
    assert paper_providers.build_paper_protocol_rail({}, ctx) is None
    assert paper_providers.build_paper_gate_audit_rail({}, ctx) is None


def test_run_dir_prefers_project_over_workspace(
    paper_providers: types.ModuleType, tmp_path: Path
) -> None:
    """解析优先级：有项目就写在项目里，否则退化到成员工作区。"""
    project_dir = tmp_path / "proj"
    workspace_root = tmp_path / "ws"
    with_project = _make_context(
        project_dir=str(project_dir),
        workspace=types.SimpleNamespace(root_path=str(workspace_root)),
    )
    assert paper_providers._resolve_paper_run_dir(with_project) == str(
        project_dir / "paper"
    )

    workspace_only = _make_context(
        workspace=types.SimpleNamespace(root_path=str(workspace_root))
    )
    assert paper_providers._resolve_paper_run_dir(workspace_only) == str(
        workspace_root / "paper"
    )


# ===========================================================================
# 3. 工具集：形状 + 端到端调用链（工具 → 内核 → 门控）
# ===========================================================================

COMPACT_TOOL_NAMES = {
    "register_artifact",
    "add_claims",
    "list_claims",
    "write_latex_section",
}


def test_ledger_tools_shape(paper_providers: types.ModuleType, tmp_path: Path) -> None:
    """精简档正好四个工具，且每个都带 openjiuwen 形状的 ``card``。"""
    ctx = _make_context(project_dir=str(tmp_path))
    tools = paper_providers.build_paper_ledger_tools({}, ctx)

    assert {tool.card.name for tool in tools} == COMPACT_TOOL_NAMES
    for tool in tools:
        assert isinstance(tool.card.description, str) and tool.card.description
        assert tool.card.input_params["type"] == "object"


def test_ledger_tool_chain_accepts_compliant_section(
    paper_providers: types.ModuleType, tmp_path: Path
) -> None:
    """真实跑一遍：登记产物 → 登记 claim → 写合规正文（落盘成功）。"""
    ctx = _make_context(project_dir=str(tmp_path))
    tools = {
        tool.card.name: tool
        for tool in paper_providers.build_paper_ledger_tools({}, ctx)
    }

    run_dir = tmp_path / "paper"
    metrics = run_dir / "exp" / "metrics.json"
    metrics.parent.mkdir(parents=True, exist_ok=True)
    metrics.write_text(
        json.dumps({"results": {"improvement_pct": 50.94, "ols_mse_mean": 0.6198}}),
        encoding="utf-8",
    )

    registered = asyncio.run(tools["register_artifact"].func(path=str(metrics), producer="test"))
    assert registered["artifact_id"]
    artifact_id = registered["artifact_id"]

    claims = asyncio.run(
        tools["add_claims"].func(
            artifact_id=artifact_id,
            claims=[
                {"text": "Relative improvement over OLS (percent).", "locator": "/results/improvement_pct"},
                {"text": "Mean OLS test MSE.", "locator": "/results/ols_mse_mean"},
            ],
        )
    )
    assert claims["registered_count"] == 2, claims
    macros = [entry["latex_macro"] for entry in claims["registered"]]

    written = asyncio.run(
        tools["write_latex_section"].func(
            relative_path="sections/experiments.tex",
            content=(
                "Ridge reduces the mean test MSE by "
                f"{macros[0]} percent, from a baseline of {macros[1]}."
            ),
        )
    )
    assert written["written"] is True
    assert written["gate_ok"] is True
    assert (run_dir / "sections" / "experiments.tex").is_file()


def test_ledger_tool_chain_rejects_handwritten_number(
    paper_providers: types.ModuleType, tmp_path: Path
) -> None:
    """同样链路上，手写结果数值的正文被拒，且**不落盘**。"""
    ctx = _make_context(project_dir=str(tmp_path))
    tools = {
        tool.card.name: tool
        for tool in paper_providers.build_paper_ledger_tools({}, ctx)
    }

    run_dir = tmp_path / "paper"
    metrics = run_dir / "exp" / "metrics.json"
    metrics.parent.mkdir(parents=True, exist_ok=True)
    metrics.write_text(json.dumps({"results": {"improvement_pct": 50.94}}), encoding="utf-8")
    registered = asyncio.run(tools["register_artifact"].func(path=str(metrics), producer="test"))
    asyncio.run(
        tools["add_claims"].func(
            artifact_id=registered["artifact_id"],
            claims=[{"text": "Improvement.", "locator": "/results/improvement_pct"}],
        )
    )

    rejected = asyncio.run(
        tools["write_latex_section"].func(
            relative_path="sections/experiments.tex",
            content="Ridge achieves 50.94 percent improvement over OLS.",
        )
    )
    assert rejected["ok"] is False
    assert rejected["rejected"] is True
    assert rejected["error_type"] == "GateRejected"
    assert not (run_dir / "sections" / "experiments.tex").exists()


def test_assembler_tool_reports_clear_error_when_unconfigured(
    paper_providers: types.ModuleType, tmp_path: Path
) -> None:
    """未配置模板/引擎时，组装工具必须**明确报错**而不是静默产出一个坏 PDF。"""
    ctx = _make_context(project_dir=str(tmp_path))
    tools = {
        tool.card.name: tool
        for tool in paper_providers.build_paper_assembler_tools({}, ctx)
    }
    assert set(tools) == {"assemble_paper_pdf", "inspect_paper_pdf"}

    result = asyncio.run(tools["assemble_paper_pdf"].func())
    assert result["ok"] is False
    assert result["error"]
    assert not list((tmp_path / "paper").glob("*.pdf"))


def test_assembler_tool_params_are_baked_from_config_specs_shape(
    paper_providers: types.ModuleType, tmp_path: Path
) -> None:
    """属性参数从 params 进入工具集（构造期烘焙，模型无法改编译环境）。"""
    ctx = _make_context(project_dir=str(tmp_path))
    params = {
        "template_dir": "T",
        "tectonic_path": "X",
        "bundle_path": "B",
        "cache_dir": "C",
        "pdf_title": "Title",
        "sections": [["intro", "Introduction"]],
        "sections_subdir": "sec",
    }
    tools = paper_providers.build_paper_assembler_tools(params, ctx)
    assert {tool.card.name for tool in tools} == {"assemble_paper_pdf", "inspect_paper_pdf"}

    inp = paper_providers.PaperAssemblerToolsInput.resolve(params, ctx)
    assert (inp.template_dir, inp.tectonic_path, inp.bundle_path) == ("T", "X", "B")
    assert inp.sections == [["intro", "Introduction"]]


# ===========================================================================
# 4. Rails：提示注入 + 绕过审计
# ===========================================================================


class _FakePromptBuilder:
    """``system_prompt_builder`` 替身（只实现 rail 用到的方法）。"""

    def __init__(self) -> None:
        self.sections: dict[str, Any] = {}

    def add_section(self, section: Any) -> None:
        self.sections[section.name] = section

    def remove_section(self, name: str) -> None:
        self.sections.pop(name, None)


def test_protocol_rail_injects_literal_macro_text(
    paper_providers: types.ModuleType, tmp_path: Path
) -> None:
    """协议 rail 注入的文本里 ``\\result{claim_id}`` 必须原样保留。

    回归点：早期实现用 ``str.format`` 填充运行目录，LaTeX 宏里的 ``{claim_id}``
    会被当成格式字段抛 ``KeyError``。本用例钉死这个行为。
    """
    ctx = _make_context(project_dir=str(tmp_path))
    rail = paper_providers.build_paper_protocol_rail({}, ctx)
    assert rail is not None

    builder = _FakePromptBuilder()
    rail.init(types.SimpleNamespace(system_prompt_builder=builder))
    asyncio.run(rail.before_model_call(AgentCallbackContext()))

    section = builder.sections["paper_evidence_protocol"]
    body = section.content["cn"]
    assert "\\result{claim_id}" in body
    assert str(tmp_path / "paper") in body
    assert "{run_dir}" not in body

    rail.uninit(types.SimpleNamespace(system_prompt_builder=builder))
    assert "paper_evidence_protocol" not in builder.sections


def test_audit_rail_flags_generic_write_with_handwritten_number(
    paper_providers: types.ModuleType, tmp_path: Path
) -> None:
    """绕过专用工具直接写 .tex 时，审计 rail 检测并留痕。"""
    ctx = _make_context(project_dir=str(tmp_path))
    rail = paper_providers.build_paper_gate_audit_rail({}, ctx)
    assert rail is not None

    run_dir = tmp_path / "paper"
    sections = run_dir / "sections"
    sections.mkdir(parents=True, exist_ok=True)

    violating = sections / "experiments.tex"
    violating.write_text("Ridge achieves 50.94 percent improvement.\n", encoding="utf-8")
    asyncio.run(
        rail.after_tool_call(
            AgentCallbackContext(
                inputs=ToolCallInputs(
                    tool_name="write_file", tool_args={"file_path": str(violating)}
                )
            )
        )
    )

    audit_path = run_dir / "gate_audit.jsonl"
    assert audit_path.is_file()
    record = json.loads(audit_path.read_text(encoding="utf-8").strip().splitlines()[0])
    assert record["violation"] is True
    assert record["suspected_handwritten_numbers"]

    compliant = sections / "intro.tex"
    compliant.write_text("Ridge improves on OLS by \\result{cl-improve}.\n", encoding="utf-8")
    asyncio.run(
        rail.after_tool_call(
            AgentCallbackContext(
                inputs=ToolCallInputs(
                    tool_name="write_file", tool_args={"file_path": str(compliant)}
                )
            )
        )
    )
    records = [
        json.loads(line)
        for line in audit_path.read_text(encoding="utf-8").strip().splitlines()
    ]
    assert records[-1]["violation"] is False
    assert records[-1]["has_result_macro"] is True


def test_audit_rail_ignores_writes_outside_run_dir(
    paper_providers: types.ModuleType, tmp_path: Path
) -> None:
    """运行目录之外的 ``.tex`` 写入不属于本次论文任务，不记录。"""
    ctx = _make_context(project_dir=str(tmp_path / "proj"))
    rail = paper_providers.build_paper_gate_audit_rail({}, ctx)
    assert rail is not None

    outside = tmp_path / "elsewhere" / "notes.tex"
    outside.parent.mkdir(parents=True, exist_ok=True)
    outside.write_text("Ridge achieves 50.94 percent improvement.\n", encoding="utf-8")

    asyncio.run(
        rail.after_tool_call(
            AgentCallbackContext(
                inputs=ToolCallInputs(
                    tool_name="write_file", tool_args={"file_path": str(outside)}
                )
            )
        )
    )
    assert not (tmp_path / "proj" / "paper" / "gate_audit.jsonl").exists()


def test_audit_rail_ignores_non_write_tools_and_non_tex(
    paper_providers: types.ModuleType, tmp_path: Path
) -> None:
    """只审计"通用写工具写 .tex"这一类调用，其余一律放过。"""
    ctx = _make_context(project_dir=str(tmp_path))
    rail = paper_providers.build_paper_gate_audit_rail({}, ctx)
    assert rail is not None

    run_dir = tmp_path / "paper"
    run_dir.mkdir(parents=True, exist_ok=True)
    note = run_dir / "notes.md"
    note.write_text("Ridge achieves 50.94 percent improvement.\n", encoding="utf-8")

    for inputs in (
        ToolCallInputs(tool_name="read_file", tool_args={"file_path": str(note)}),
        ToolCallInputs(tool_name="write_file", tool_args={"file_path": str(note)}),
        ToolCallInputs(tool_name="write_file", tool_args={}),
    ):
        asyncio.run(rail.after_tool_call(AgentCallbackContext(inputs=inputs)))

    assert not (run_dir / "gate_audit.jsonl").exists()
