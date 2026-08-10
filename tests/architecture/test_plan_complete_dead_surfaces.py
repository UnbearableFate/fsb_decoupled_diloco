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


def test_old_selection_and_compatibility_api_are_deleted() -> None:
    merge = _top_level_definitions("fs_diloco/protocol/merge.py")
    leader = _class_definitions("fs_diloco/storage/authority.py", "LeaderSession")
    admission = _class_definitions("fs_diloco/protocol/authority.py", "DynamicAdmission")

    assert {"select_one_per_learner", "stale_update_ids"}.isdisjoint(merge)
    assert "record_proposal" not in leader
    assert {
        "resume_cursor",
        "last_receipt_id",
        "last_receipt_sha256",
        "next_cycle_seq",
    }.isdisjoint(admission)


def test_early_development_layout_fallbacks_are_deleted() -> None:
    syncer = (ROOT / "fs_diloco/runtime/syncer.py").read_text(encoding="utf-8")
    admission = (ROOT / "fs_diloco/storage/admission.py").read_text(encoding="utf-8")

    assert "canonical_receipt_id" not in syncer
    assert "admissions/{learner_id}" not in admission


def test_only_unversioned_product_surfaces_exist() -> None:
    tracked = {
        path.relative_to(ROOT).as_posix()
        for root in (ROOT / "fs_diloco", ROOT / "configs", ROOT / "scripts", ROOT / "tests")
        for path in root.rglob("*")
        if path.is_file()
    }
    generation_suffix = re.compile(r"(?:^|[/_.-])v[0-9]+(?:$|[/_.-])")
    assert not any(generation_suffix.search(path) for path in tracked)


def test_versions_and_paths_have_one_canonical_owner() -> None:
    constants = (ROOT / "fs_diloco/core/constants.py").read_text(encoding="utf-8")
    param_index = (ROOT / "fs_diloco/modeling/param_index.py").read_text(encoding="utf-8")
    paths = _class_definitions("fs_diloco/storage/paths.py", "RunPaths")
    path_functions = _top_level_definitions("fs_diloco/storage/paths.py")
    atomic_functions = _top_level_definitions("fs_diloco/storage/atomic_io.py")

    assert "PROTOCOL_VERSION" not in constants
    assert "FORMAT_VERSION" not in constants
    assert "PARAM_INDEX_FORMAT_VERSION" in param_index
    assert "core.constants import FORMAT_VERSION" not in param_index
    assert {"global_weight_path", "outer_optim_path"}.isdisjoint(paths)
    assert {
        "iter_instance_heartbeats",
        "iter_instance_pointers",
        "iter_instance_payloads",
        "iter_registration_requests",
    }.isdisjoint(paths)
    assert "prepare_run_dirs" not in path_functions
    assert "wait_for_file" not in atomic_functions
