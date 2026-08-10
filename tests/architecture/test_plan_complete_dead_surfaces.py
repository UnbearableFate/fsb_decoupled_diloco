from __future__ import annotations

import ast
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def _top_level_definitions(relative: str) -> set[str]:
    tree = ast.parse((ROOT / relative).read_text(encoding="utf-8"))
    return {
        node.name
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    }


def _class_definitions(relative: str, class_name: str) -> set[str]:
    tree = ast.parse((ROOT / relative).read_text(encoding="utf-8"))
    cls = next(
        node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == class_name
    )
    return {
        node.name for node in cls.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


def _class_annotated_fields(relative: str, class_name: str) -> tuple[str, ...]:
    tree = ast.parse((ROOT / relative).read_text(encoding="utf-8"))
    cls = next(
        node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == class_name
    )
    return tuple(
        node.target.id
        for node in cls.body
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name)
    )


def test_merge_and_admission_modules_expose_the_current_protocol_surface() -> None:
    merge = _top_level_definitions("fs_diloco/protocol/merge.py")
    admission_fields = _class_annotated_fields(
        "fs_diloco/protocol/authority.py", "DynamicAdmission"
    )

    assert merge == {
        "staleness",
        "raw_update_weight",
        "normalized_update_weights",
        "weighted_average_tensors",
    }
    assert admission_fields == ("fence", "resume")


def test_receipt_identity_and_paths_have_one_protocol_owner() -> None:
    receipt = _top_level_definitions("fs_diloco/protocol/cycle_receipt.py")
    syncer = (ROOT / "fs_diloco/runtime/syncer.py").read_text(encoding="utf-8")
    admission = (ROOT / "fs_diloco/storage/admission.py").read_text(encoding="utf-8")

    assert {
        "canonical_receipt_id",
        "contributor_fence_namespace",
        "canonical_receipt_relative_path",
    }.issubset(receipt)
    assert "canonical_receipt_id" not in syncer
    assert "canonical_receipt_id" not in admission


def test_only_unversioned_product_surfaces_exist() -> None:
    tracked = {
        path.relative_to(ROOT).as_posix()
        for root in (ROOT / "fs_diloco", ROOT / "configs", ROOT / "scripts", ROOT / "tests")
        for path in root.rglob("*")
        if path.is_file()
    }
    generation_suffix = re.compile(r"(?:^|[/_.-])v[0-9]+(?:$|[/_.-])")
    assert not any(generation_suffix.search(path) for path in tracked)


def test_artifact_versions_and_run_paths_have_explicit_owners() -> None:
    constants = ast.parse((ROOT / "fs_diloco/core/constants.py").read_text(encoding="utf-8"))
    versions = ast.parse((ROOT / "fs_diloco/core/versions.py").read_text(encoding="utf-8"))
    paths = _class_definitions("fs_diloco/storage/paths.py", "RunPaths")
    path_functions = _top_level_definitions("fs_diloco/storage/paths.py")
    atomic_functions = _top_level_definitions("fs_diloco/storage/atomic_io.py")
    constant_names = {
        target.id
        for node in constants.body
        if isinstance(node, ast.Assign)
        for target in node.targets
        if isinstance(target, ast.Name)
    }
    version_names = {
        target.id
        for node in versions.body
        if isinstance(node, ast.Assign)
        for target in node.targets
        if isinstance(target, ast.Name)
    }

    assert constant_names == {"DEFAULT_RUNS_DIR"}
    assert {"PROTOCOL_VERSION", "CONFIG_SCHEMA_VERSION", "PARAM_INDEX_FORMAT_VERSION"} <= (
        version_names
    )
    assert {
        "bootstrap_complete_json",
        "run_descriptor_json",
        "resolved_config_yaml",
        "sqlite_db",
        "terminal_close_request_json",
    } <= paths
    assert path_functions == {"RunPaths", "prepare_authority_dirs", "prepare_learner_instance_dir"}
    assert {
        "atomic_write_bytes",
        "publish_immutable_bytes",
        "read_json",
        "safe_read_json",
        "sha256_file",
    } <= atomic_functions
