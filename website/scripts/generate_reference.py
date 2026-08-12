"""Generate deterministic API-reference data from the current fs_diloco source tree."""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any, Iterable


MODULE_SUMMARIES = {
    "fs_diloco": "定义基于共享文件系统的 Decoupled DiLoCo Python 包。",
    "fs_diloco.core": "集中提供配置、源码身份、运行描述符和制品版本契约。",
    "fs_diloco.core.adoption_rules": "为配置加载和运行期全局版本采纳策略提供不依赖 Torch 的校验。",
    "fs_diloco.core.config": "定义 Full Protocol 的严格配置模型、加载、解析和跨字段校验。",
    "fs_diloco.core.constants": "保存文件系统 DiLoCo 原型跨模块共享的常量。",
    "fs_diloco.core.run_descriptor": "读取并验证不可变运行描述符、源码身份和配置身份。",
    "fs_diloco.core.source_identity": "计算运行源码范围、Git 状态和内容指纹。",
    "fs_diloco.core.versions": "定义可独立演进的 Full Protocol 制品版本号。",
    "fs_diloco.learner": "提供强制 fenced Learner 运行时的公开、无 Torch 启动入口。",
    "fs_diloco.modeling": "集中提供模型、数据集、参数索引和优化器实现。",
    "fs_diloco.modeling.hf_data": "加载训练数据并构造可恢复的无限批次迭代器。",
    "fs_diloco.modeling.hf_identity": "拒绝不满足不可变身份要求的 Hugging Face 本地引用。",
    "fs_diloco.modeling.hf_model": "构造 Hugging Face 因果语言模型和合成冒烟测试模型。",
    "fs_diloco.modeling.outer_optim": "对扁平全局参数执行显式外层优化器更新。",
    "fs_diloco.modeling.param_index": "建立确定性的可训练参数索引，并在模型参数与扁平向量之间转换。",
    "fs_diloco.modeling.training": "构造 Full Protocol Learner 使用的内层优化器和学习率调度器。",
    "fs_diloco.observability": "提供结构化进程日志和不可变 Actor 遥测。",
    "fs_diloco.observability.logging_utils": "将带有唯一 Actor 身份的运行证明写入 JSONL。",
    "fs_diloco.protocol": "定义不依赖运行时和存储实现的 Full Protocol 值对象。",
    "fs_diloco.protocol._validation": "提供协议边界使用的严格、无外部依赖校验原语。",
    "fs_diloco.protocol.authority": "定义跨越 authority 边界的类型化应用对象。",
    "fs_diloco.protocol.contributor": "定义唯一的 contributor fence 和固定 stream-pool membership scope。",
    "fs_diloco.protocol.cycle_receipt": "定义 CycleReceiptV1 的严格序列化边界。",
    "fs_diloco.protocol.data_cursor": "定义索引化或物化训练数据的确定性游标和恢复状态。",
    "fs_diloco.protocol.merge": "计算 proposal 的 token/陈旧度权重并执行张量加权平均。",
    "fs_diloco.protocol.proposal": "定义 FullUpdateProposalV2 的严格序列化边界。",
    "fs_diloco.protocol.scheduler": "定义 scheduler uncertainty 状态和 operator request 边界。",
    "fs_diloco.protocol.token_accounting": "执行 Learner segment 与 authority token 的确定性记账。",
    "fs_diloco.runtime": "组合 Learner 与 Syncer 进程运行时。",
    "fs_diloco.runtime.adoption": "实现 Full Learner 的全局版本采纳状态机。",
    "fs_diloco.runtime.learner": "执行通过 admission 后的 Full Protocol 本地训练循环。",
    "fs_diloco.runtime.learner_control": "在不导入 Torch 的阶段决定 Learner 循环控制行为。",
    "fs_diloco.runtime.learner_entrypoint": "执行 Full Protocol Learner 的无 Torch admission 入口。",
    "fs_diloco.runtime.pbs_scheduler": "查询和提交容量控制使用的 PBS 作业。",
    "fs_diloco.runtime.services": "汇集 Full Protocol 运行时组合的窄职责有状态服务。",
    "fs_diloco.runtime.services.dynamic_capacity": "持久化 capacity 观测并与 PBS launch 状态对账。",
    "fs_diloco.runtime.services.maintenance": "执行带 fence 的在线归档、压缩和身份校验制品清理。",
    "fs_diloco.runtime.services.merge": "为正常路径与终态路径提供唯一的合并和发布实现。",
    "fs_diloco.runtime.services.terminal": "执行 Full Protocol 的关闭、drain 和受限最终合并。",
    "fs_diloco.runtime.syncer": "组合 fenced Syncer 的服务、合并循环和终态流程。",
    "fs_diloco.runtime.syncer_entrypoint": "验证候选身份、获取 leader lease 并启动 Syncer。",
    "fs_diloco.storage": "汇集 Full Protocol 的持久化适配器。",
    "fs_diloco.storage.admission": "在 Learner 导入 Torch 前提供文件系统 admission 请求和响应边界。",
    "fs_diloco.storage.artifact_policy": "分类版本化运行制品并限制通用清理范围。",
    "fs_diloco.storage.atomic_io": "提供原子写入、不可变发布和安全读取的文件系统原语。",
    "fs_diloco.storage.audit_archive": "发布不可变 authority 审计批次并执行离线去重。",
    "fs_diloco.storage.authority": "初始化 SQLite authority，并提供类型化读取与显式 fenced command。",
    "fs_diloco.storage.control": "发布 epoch-scoped 控制对象；固定路径只作为可修复缓存。",
    "fs_diloco.storage.leader_lease": "定义 leader lease 值对象和过期 fencing 错误。",
    "fs_diloco.storage.object_store": "按 authority 内容身份消费不可变制品。",
    "fs_diloco.storage.paths": "集中构造运行根目录中的规范路径。",
    "fs_diloco.storage.run_initializer": "以 crash-recoverable、no-replace 方式发布新的共享文件系统运行根目录。",
    "fs_diloco.storage.tensor_codec": "使用 Safetensors 编码并按内容身份读取全局权重、更新向量与外层优化器状态。",
    "fs_diloco.storage.tensor_identity": "提供不依赖存储和建模层的底层张量身份计算。",
    "fs_diloco.storage.terminal_request": "发布和读取 operator 发起的不可变终态关闭请求。",
    "fs_diloco.syncer": "提供强制 fenced Syncer candidate 运行时的公开入口。",
    "fs_diloco.tools": "汇集运行初始化、检查、operator request 和清理工具。",
    "fs_diloco.tools.analysis": "通过 durable authority 检查一个当前 Full Protocol 运行。",
    "fs_diloco.tools.clean_run": "保守地筛选并删除已完成运行中的冗余输出。",
    "fs_diloco.tools.init_run": "在提交 PBS Actor 前初始化不可变 Full Protocol 运行。",
    "fs_diloco.tools.launch_independent_run": "初始化运行并提交相互独立的 Full Protocol PBS Actor。",
    "fs_diloco.tools.request_terminal_close": "为 Full Protocol 运行发布一份不可变 manual-close request。",
    "fs_diloco.tools.resolve_scheduler_uncertainty": "创建用于解决 scheduler uncertainty 的不可变 operator request。",
}


TOKEN_LABELS = {
    "active": "活跃状态",
    "actor": "Actor",
    "admission": "准入",
    "adopt": "采纳",
    "adoption": "采纳",
    "artifact": "制品",
    "audit": "审计",
    "authority": "权威状态",
    "batch": "批次",
    "binding": "绑定",
    "block": "数据块",
    "capacity": "容量",
    "checkpoint": "检查点",
    "close": "关闭",
    "command": "命令",
    "committed": "已提交版本",
    "config": "配置",
    "control": "控制发布",
    "contributor": "Contributor",
    "cursor": "游标",
    "cycle": "Cycle",
    "data": "数据",
    "dataset": "数据集",
    "descriptor": "运行描述符",
    "drain": "drain 状态",
    "dynamic": "Dynamic",
    "epoch": "Epoch",
    "fence": "Fence",
    "file": "文件",
    "global": "全局版本",
    "heartbeat": "Heartbeat",
    "identity": "身份",
    "index": "索引",
    "learner": "Learner",
    "lease": "Lease",
    "latest": "最新版本",
    "launch": "启动请求",
    "leader": "Leader",
    "log": "日志",
    "manifest": "Manifest",
    "membership": "成员资格",
    "merge": "合并",
    "model": "模型",
    "object": "对象",
    "operator": "Operator",
    "optim": "优化器状态",
    "optimizer": "优化器",
    "outer": "外层优化器",
    "param": "参数",
    "parameter": "参数",
    "path": "路径",
    "payload": "Payload",
    "policy": "策略",
    "prediction": "预测状态",
    "proposal": "Proposal",
    "publication": "发布",
    "receipt": "Receipt",
    "request": "请求",
    "response": "响应",
    "resume": "恢复状态",
    "run": "运行",
    "scheduler": "调度器",
    "selection": "选择批次",
    "source": "源码",
    "state": "状态",
    "syncer": "Syncer",
    "tensor": "张量",
    "terminal": "终态",
    "token": "Token",
    "tokens": "Token",
    "training": "训练",
    "update": "更新",
    "version": "版本",
    "weight": "权重",
    "weights": "权重",
}


SPECIAL_MEMBER_SUMMARIES = {
    "__init__": "初始化实例及其运行期状态。",
    "__post_init__": "在 dataclass 初始化后校验字段不变量。",
    "as_dict": "将实例编码为可序列化字典。",
    "from_dict": "从字典边界解码并校验实例。",
    "from_json": "从 JSON 边界解码并校验实例。",
    "canonical_bytes": "生成确定性的规范字节表示。",
    "immutable_sha256": "计算规范不可变内容的 SHA-256 身份。",
    "stable_contributor_key": "返回跨 attempt 保持稳定的 contributor key。",
    "stable_key": "返回用于确定性选择或索引的稳定 key。",
    "main": "实现该模块的命令行入口。",
}


def _unparse(node: ast.AST | None, *, fallback: str = "") -> str:
    """Return a compact source representation for one AST node."""

    if node is None:
        return fallback
    try:
        return ast.unparse(node)
    except (AttributeError, ValueError):
        return fallback


def _compact(value: str, *, limit: int = 220) -> str:
    """Collapse whitespace and cap generated inline source fragments."""

    collapsed = " ".join(value.split())
    return collapsed if len(collapsed) <= limit else f"{collapsed[: limit - 1]}…"


def _module_identity(repo_root: Path, path: Path) -> tuple[str, str, bool]:
    """Resolve import name, documentation route, and package status for a source file."""

    relative = path.relative_to(repo_root).with_suffix("")
    parts = list(relative.parts)
    is_package = parts[-1] == "__init__"
    if is_package:
        parts.pop()
    module_name = ".".join(parts)
    route = "/reference/" + "/".join(parts)
    return module_name, route, is_package


def _visibility(name: str) -> str:
    """Classify a Python identifier as public, protected, or dunder."""

    if name.startswith("__") and name.endswith("__"):
        return "dunder"
    if name.startswith("_"):
        return "internal"
    return "public"


def _friendly_object(name: str) -> str:
    """Translate common identifier tokens without altering the identifier itself."""

    tokens = [item for item in name.strip("_").split("_") if item]
    translated = [TOKEN_LABELS.get(token, "") for token in tokens]
    known = [item for item in translated if item]
    if known:
        return "、".join(dict.fromkeys(known))
    return f"`{name}` 对应的数据"


def _callable_summary(name: str, kind: str) -> str:
    """Create a conservative Chinese responsibility statement from a callable name."""

    if name in SPECIAL_MEMBER_SUMMARIES:
        return SPECIAL_MEMBER_SUMMARIES[name]
    clean = name.strip("_")
    target = _friendly_object(clean)
    prefix = clean.split("_", 1)[0]
    if kind == "property":
        return f"读取 {target}。"
    actions = {
        "archive": f"归档{target}。",
        "build": f"构建{target}。",
        "calculate": f"计算{target}。",
        "capture": f"采集并固化{target}。",
        "choose": f"选择{target}。",
        "claim": f"声明{target}的处理权。",
        "clean": f"清理{target}。",
        "commit": f"提交{target}并推进权威状态。",
        "complete": f"完成并记录{target}的处置。",
        "compute": f"计算{target}。",
        "create": f"创建{target}。",
        "decode": f"从边界表示解码并校验{target}。",
        "dispose": f"处置{target}并记录结果。",
        "encode": f"将{target}编码为存储或传输表示。",
        "ensure": f"确保{target}满足当前不变量。",
        "finalize": f"完成{target}的终态处理。",
        "flatten": f"将{target}转换为确定性扁平表示。",
        "init": f"初始化{target}。",
        "initialize": f"初始化{target}。",
        "ingest": f"摄取并校验{target}。",
        "is": f"返回{target}的布尔判定。",
        "iter": f"按确定性顺序迭代{target}。",
        "load": f"读取并校验{target}。",
        "mark": f"更新{target}的状态标记。",
        "normalize": f"规范化{target}。",
        "open": f"打开并校验{target}。",
        "parse": f"解析并校验{target}。",
        "prepare": f"准备{target}，但不将其视为已提交事实。",
        "publish": f"发布{target}。",
        "read": f"读取并校验{target}。",
        "reconcile": f"将{target}与权威状态对账。",
        "record": f"记录{target}。",
        "reject": f"拒绝不满足身份或格式约束的{target}。",
        "request": f"创建或发布{target}。",
        "resolve": f"解析{target}并生成规范结果。",
        "run": f"运行{target}对应的流程。",
        "safe": f"以失败关闭方式读取或处理{target}。",
        "save": f"保存{target}。",
        "select": f"按当前约束选择{target}。",
        "validate": f"校验{target}；不满足约束时抛出异常。",
        "wait": f"等待{target}达到可继续处理的状态。",
        "weighted": f"按权重计算{target}。",
        "write": f"写入{target}。",
    }
    return actions.get(prefix, f"实现 `{name}` 对应的模块内操作。")


def _field_summary(name: str, annotation: str, default: str | None) -> str:
    """Describe a stored value without inventing semantics absent from its declaration."""

    clean = name.strip("_")
    target = _friendly_object(clean)
    if clean.endswith("_seconds"):
        return f"保存{target}的时间边界，单位为秒。"
    if clean.endswith("_ms"):
        return f"保存{target}的时间边界，单位为毫秒。"
    if clean.endswith("_sha256") or clean == "sha256":
        return f"保存{target}的 SHA-256 内容身份。"
    if clean.endswith("_path") or clean.endswith("_root"):
        return f"保存{target}的路径。"
    if clean.startswith(("is_", "has_", "allow_", "enable_", "enabled")) or annotation == "bool":
        return f"控制或记录是否启用{target}。"
    if clean.endswith("_id") or clean == "id":
        return f"保存{target}的稳定标识。"
    if clean.endswith("_version") or clean == "version":
        return f"保存{target}的版本号。"
    if clean.endswith("_state") or clean in {"state", "status"}:
        return f"保存{target}的当前状态。"
    suffix = f"默认值为 `{default}`。" if default is not None else ""
    return f"保存{target}。{suffix}".strip()


def _parameter_summary(name: str, annotation: str, default: str | None) -> str:
    """Describe one callable parameter from its stable declaration facts."""

    if name in {"self", "cls"}:
        return "当前实例。"
    target = _friendly_object(name)
    default_text = f"省略时使用 `{default}`。" if default is not None else ""
    return f"输入的{target}。{default_text}".strip()


def _parameter_docs(arguments: ast.arguments) -> list[dict[str, Any]]:
    """Convert Python argument nodes into PyTorch-style parameter records."""

    records: list[dict[str, Any]] = []
    positional = list(arguments.posonlyargs) + list(arguments.args)
    padded_defaults: list[ast.expr | None] = [None] * (
        len(positional) - len(arguments.defaults)
    ) + list(arguments.defaults)
    for index, (argument, default_node) in enumerate(zip(positional, padded_defaults, strict=True)):
        if argument.arg in {"self", "cls"}:
            continue
        default = _unparse(default_node) if default_node is not None else None
        annotation = _unparse(argument.annotation, fallback="Any")
        kind = "positional-only" if index < len(arguments.posonlyargs) else "positional-or-keyword"
        records.append(
            {
                "name": argument.arg,
                "type": annotation,
                "default": default,
                "kind": kind,
                "description": _parameter_summary(argument.arg, annotation, default),
            }
        )
    if arguments.vararg is not None:
        annotation = _unparse(arguments.vararg.annotation, fallback="Any")
        records.append(
            {
                "name": f"*{arguments.vararg.arg}",
                "type": annotation,
                "default": None,
                "kind": "variadic-positional",
                "description": f"额外的位置参数；单项类型为 `{annotation}`。",
            }
        )
    for argument, default_node in zip(arguments.kwonlyargs, arguments.kw_defaults, strict=True):
        default = _unparse(default_node) if default_node is not None else None
        annotation = _unparse(argument.annotation, fallback="Any")
        records.append(
            {
                "name": argument.arg,
                "type": annotation,
                "default": default,
                "kind": "keyword-only",
                "description": _parameter_summary(argument.arg, annotation, default),
            }
        )
    if arguments.kwarg is not None:
        annotation = _unparse(arguments.kwarg.annotation, fallback="Any")
        records.append(
            {
                "name": f"**{arguments.kwarg.arg}",
                "type": annotation,
                "default": None,
                "kind": "variadic-keyword",
                "description": f"额外的关键字参数；值类型为 `{annotation}`。",
            }
        )
    return records


def _callable_signature(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    """Render a callable signature without copying its implementation body."""

    prefix = "async " if isinstance(node, ast.AsyncFunctionDef) else ""
    returns = _unparse(node.returns)
    suffix = f" -> {returns}" if returns else ""
    return f"{prefix}{node.name}({_unparse(node.args)}){suffix}"


def _body_nodes_without_nested(nodes: Iterable[ast.stmt]) -> Iterable[ast.AST]:
    """Yield body nodes while excluding nested callable and class implementations."""

    pending: list[ast.AST] = list(nodes)
    while pending:
        current = pending.pop()
        yield current
        if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
            continue
        pending.extend(ast.iter_child_nodes(current))


def _direct_raises(node: ast.FunctionDef | ast.AsyncFunctionDef) -> list[str]:
    """Collect exceptions raised directly by one callable body."""

    names: list[str] = []
    for child in _body_nodes_without_nested(node.body):
        if not isinstance(child, ast.Raise):
            continue
        if child.exc is None:
            name = "当前异常"
        elif isinstance(child.exc, ast.Call):
            name = _unparse(child.exc.func, fallback="动态异常")
        else:
            name = _unparse(child.exc, fallback="动态异常")
        if name not in names:
            names.append(name)
    return names


def _return_summary(annotation: str) -> str:
    """Create a conservative return-value explanation from one annotation."""

    if not annotation or annotation == "None":
        return "无返回值；成功时仅完成对应状态变更。"
    if annotation == "bool":
        return "返回布尔判定结果。"
    if "Path" in annotation:
        return f"返回 `{annotation}` 类型的规范路径结果。"
    return f"返回 `{annotation}` 类型的结果。"


def _callable_doc(
    node: ast.FunctionDef | ast.AsyncFunctionDef, *, owner: str | None = None
) -> dict[str, Any]:
    """Build a complete documentation record for a function or method."""

    decorators = [_unparse(item) for item in node.decorator_list]
    kind = "method" if owner else "function"
    if any(item == "property" or item.endswith(".property") for item in decorators):
        kind = "property"
    elif any(item == "classmethod" or item.endswith(".classmethod") for item in decorators):
        kind = "classmethod"
    elif any(item == "staticmethod" or item.endswith(".staticmethod") for item in decorators):
        kind = "staticmethod"
    return_annotation = _unparse(node.returns, fallback="Any")
    return {
        "name": node.name,
        "qualifiedName": f"{owner}.{node.name}" if owner else node.name,
        "kind": kind,
        "visibility": _visibility(node.name),
        "signature": _callable_signature(node),
        "summary": _callable_summary(node.name, kind),
        "docstring": ast.get_docstring(node, clean=True),
        "parameters": _parameter_docs(node.args),
        "returns": {"type": return_annotation, "description": _return_summary(return_annotation)},
        "raises": _direct_raises(node),
        "decorators": decorators,
        "line": node.lineno,
        "endLine": node.end_lineno or node.lineno,
    }


def _assignment_records(node: ast.Assign | ast.AnnAssign, *, kind: str) -> list[dict[str, Any]]:
    """Convert simple name assignments into module or class member records."""

    targets: list[ast.expr]
    value: ast.expr | None
    annotation: ast.expr | None = None
    if isinstance(node, ast.AnnAssign):
        targets = [node.target]
        value = node.value
        annotation = node.annotation
    else:
        targets = list(node.targets)
        value = node.value
    records: list[dict[str, Any]] = []
    for target in targets:
        names: list[str] = []
        if isinstance(target, ast.Name):
            names = [target.id]
        elif isinstance(target, (ast.Tuple, ast.List)):
            names = [item.id for item in target.elts if isinstance(item, ast.Name)]
        for name in names:
            value_text = _compact(_unparse(value)) if value is not None else None
            annotation_text = _unparse(annotation, fallback="Any")
            records.append(
                {
                    "name": name,
                    "kind": kind,
                    "visibility": _visibility(name),
                    "type": annotation_text,
                    "default": value_text,
                    "summary": _field_summary(name, annotation_text, value_text),
                    "line": node.lineno,
                }
            )
    return records


def _instance_fields(node: ast.ClassDef) -> list[dict[str, Any]]:
    """Collect instance attributes assigned through self inside __init__."""

    initializer = next(
        (
            item
            for item in node.body
            if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and item.name == "__init__"
        ),
        None,
    )
    if initializer is None:
        return []
    records: list[dict[str, Any]] = []
    seen: set[str] = set()
    for child in _body_nodes_without_nested(initializer.body):
        target: ast.expr | None = None
        value: ast.expr | None = None
        annotation: ast.expr | None = None
        if isinstance(child, ast.Assign) and len(child.targets) == 1:
            target = child.targets[0]
            value = child.value
        elif isinstance(child, ast.AnnAssign):
            target = child.target
            value = child.value
            annotation = child.annotation
        if not (
            isinstance(target, ast.Attribute)
            and isinstance(target.value, ast.Name)
            and target.value.id == "self"
            and target.attr not in seen
        ):
            continue
        seen.add(target.attr)
        value_text = _compact(_unparse(value)) if value is not None else None
        annotation_text = _unparse(annotation, fallback="Any")
        records.append(
            {
                "name": target.attr,
                "kind": "instance-field",
                "visibility": _visibility(target.attr),
                "type": annotation_text,
                "default": value_text,
                "summary": _field_summary(target.attr, annotation_text, value_text),
                "line": child.lineno,
            }
        )
    return records


def _class_doc(node: ast.ClassDef) -> dict[str, Any]:
    """Build a complete documentation record for a top-level class."""

    decorators = [_unparse(item) for item in node.decorator_list]
    bases = [_unparse(item) for item in node.bases]
    class_fields: list[dict[str, Any]] = []
    methods: list[dict[str, Any]] = []
    for child in node.body:
        if isinstance(child, (ast.Assign, ast.AnnAssign)):
            class_fields.extend(_assignment_records(child, kind="class-field"))
        elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
            methods.append(_callable_doc(child, owner=node.name))
    known = {item["name"] for item in class_fields}
    class_fields.extend(item for item in _instance_fields(node) if item["name"] not in known)
    is_dataclass = any(item == "dataclass" or item.endswith(".dataclass") for item in decorators)
    is_enum = any(item.endswith("Enum") for item in bases)
    is_error = node.name.endswith(("Error", "Exception")) or any(
        item.endswith(("Error", "Exception")) for item in bases
    )
    if is_error:
        summary = f"表示 `{node.name}` 对应的失败条件。"
    elif is_enum:
        summary = f"定义 `{node.name}` 支持的状态或动作枚举值。"
    elif is_dataclass:
        summary = f"表示 `{node.name}` 结构化值，并集中保存下列字段。"
    else:
        summary = f"封装 `{node.name}` 的状态与操作。"
    signature = f"class {node.name}"
    if bases:
        signature += f"({', '.join(bases)})"
    return {
        "name": node.name,
        "kind": "class",
        "visibility": _visibility(node.name),
        "signature": signature,
        "summary": summary,
        "docstring": ast.get_docstring(node, clean=True),
        "bases": bases,
        "decorators": decorators,
        "fields": class_fields,
        "methods": methods,
        "line": node.lineno,
        "endLine": node.end_lineno or node.lineno,
    }


def _resolve_import(module_name: str, is_package: bool, node: ast.ImportFrom) -> str:
    """Resolve one relative import to its absolute module name."""

    if node.level == 0:
        return node.module or ""
    package = module_name if is_package else module_name.rpartition(".")[0]
    parts = package.split(".") if package else []
    trim = max(node.level - 1, 0)
    if trim:
        parts = parts[:-trim]
    if node.module:
        parts.extend(node.module.split("."))
    return ".".join(parts)


def _raw_dependencies(tree: ast.Module, *, module_name: str, is_package: bool) -> list[str]:
    """Collect fs_diloco import targets from module-level and deferred imports."""

    values: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            values.update(alias.name for alias in node.names if alias.name.startswith("fs_diloco"))
        elif isinstance(node, ast.ImportFrom):
            resolved = _resolve_import(module_name, is_package, node)
            if resolved.startswith("fs_diloco"):
                values.add(resolved)
    values.discard(module_name)
    return sorted(values)


def _module_record(repo_root: Path, path: Path) -> dict[str, Any]:
    """Parse one source file into a module documentation record."""

    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))
    module_name, route, is_package = _module_identity(repo_root, path)
    classes: list[dict[str, Any]] = []
    functions: list[dict[str, Any]] = []
    variables: list[dict[str, Any]] = []
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            classes.append(_class_doc(node))
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            functions.append(_callable_doc(node))
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            variables.extend(_assignment_records(node, kind="module-variable"))
    exports: list[str] = []
    for item in variables:
        if item["name"] == "__all__" and item["default"]:
            try:
                parsed = ast.literal_eval(item["default"])
            except (SyntaxError, ValueError):
                parsed = []
            if isinstance(parsed, (list, tuple)) and all(
                isinstance(value, str) for value in parsed
            ):
                exports = list(parsed)
    relative_path = path.relative_to(repo_root).as_posix()
    return {
        "module": module_name,
        "route": route,
        "sourcePath": relative_path,
        "sourceSha256": hashlib.sha256(source.encode("utf-8")).hexdigest(),
        "isPackage": is_package,
        "visibility": "internal"
        if any(part.startswith("_") for part in module_name.split("."))
        else "public",
        "summary": MODULE_SUMMARIES.get(module_name, f"提供 `{module_name}` 的当前实现。"),
        "docstring": ast.get_docstring(tree, clean=True),
        "exports": exports,
        "variables": variables,
        "classes": classes,
        "functions": functions,
        "rawDependencies": _raw_dependencies(tree, module_name=module_name, is_package=is_package),
        "dependencies": [],
        "usedBy": [],
        "lineCount": len(source.splitlines()),
    }


def _source_revision(repo_root: Path) -> str:
    """Return the latest commit that changed the documented Python source tree."""

    return subprocess.run(
        ["git", "log", "-1", "--format=%H", "--", "fs_diloco"],
        cwd=repo_root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _link_dependencies(modules: list[dict[str, Any]]) -> None:
    """Resolve import targets to documented modules and populate reverse users."""

    by_name = {item["module"]: item for item in modules}
    names = sorted(by_name, key=len, reverse=True)
    for item in modules:
        resolved: set[str] = set()
        for target in item.pop("rawDependencies"):
            match = next(
                (name for name in names if target == name or target.startswith(f"{name}.")),
                None,
            )
            if match is not None and match != item["module"]:
                resolved.add(match)
        item["dependencies"] = sorted(resolved)
    for item in modules:
        for dependency in item["dependencies"]:
            by_name[dependency]["usedBy"].append(item["module"])
    for item in modules:
        item["usedBy"].sort()


def _statistics(modules: list[dict[str, Any]]) -> dict[str, int]:
    """Count documented modules and member kinds for coverage reporting."""

    classes = sum(len(item["classes"]) for item in modules)
    functions = sum(len(item["functions"]) for item in modules)
    methods = sum(len(class_item["methods"]) for item in modules for class_item in item["classes"])
    fields = sum(len(class_item["fields"]) for item in modules for class_item in item["classes"])
    variables = sum(len(item["variables"]) for item in modules)
    return {
        "modules": len(modules),
        "packages": sum(bool(item["isPackage"]) for item in modules),
        "classes": classes,
        "functions": functions,
        "methods": methods,
        "fields": fields,
        "variables": variables,
        "symbols": classes + functions + methods + fields + variables,
    }


def _write_index(path: Path, modules: list[dict[str, Any]], stats: dict[str, int]) -> None:
    """Write the lightweight client-side module index used by search and navigation."""

    records = [
        {
            "module": item["module"],
            "route": item["route"],
            "summary": item["summary"],
            "isPackage": item["isPackage"],
            "visibility": item["visibility"],
            "package": item["module"] if item["isPackage"] else item["module"].rpartition(".")[0],
            "counts": {
                "classes": len(item["classes"]),
                "functions": len(item["functions"]),
                "methods": sum(len(class_item["methods"]) for class_item in item["classes"]),
            },
        }
        for item in modules
    ]
    payload = json.dumps(records, ensure_ascii=False, indent=2)
    stats_payload = json.dumps(stats, ensure_ascii=False, indent=2)
    path.write_text(
        "// Generated by scripts/generate_reference.py; do not edit manually.\n"
        f"export const apiModuleIndex = {payload} as const;\n\n"
        f"export const apiReferenceStats = {stats_payload} as const;\n",
        encoding="utf-8",
    )


def generate(repo_root: Path, site_root: Path) -> dict[str, int]:
    """Generate full and lightweight reference artifacts for one repository checkout."""

    source_root = repo_root / "fs_diloco"
    source_paths = sorted(source_root.rglob("*.py"))
    modules = [_module_record(repo_root, path) for path in source_paths]
    routes = [item["route"] for item in modules]
    if len(routes) != len(set(routes)):
        raise RuntimeError("reference routes must be unique")
    _link_dependencies(modules)
    stats = _statistics(modules)
    manifest = {
        "schemaVersion": 1,
        "sourceRevision": _source_revision(repo_root),
        "stats": stats,
        "modules": modules,
    }
    data_dir = site_root / "app" / "reference-data"
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "api-manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    _write_index(data_dir / "api-index.ts", modules, stats)
    return stats


def main() -> None:
    """Parse command-line paths and generate the checked-in reference artifacts."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--site-root", type=Path, default=Path(__file__).resolve().parents[1])
    arguments = parser.parse_args()
    stats = generate(arguments.repo_root.resolve(), arguments.site_root.resolve())
    print(json.dumps(stats, sort_keys=True))


if __name__ == "__main__":
    main()
