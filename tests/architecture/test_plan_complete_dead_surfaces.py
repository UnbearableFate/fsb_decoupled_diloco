"""Verify that the current product exposes one unversioned protocol surface."""

from __future__ import annotations

import ast
import re
import subprocess
import tomllib
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def _top_level_definitions(relative: str) -> set[str]:
    """Return top-level class and function names from one repository module."""

    tree = ast.parse((ROOT / relative).read_text(encoding="utf-8"))
    return {
        node.name
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    }


def _string_literals(relative: str) -> set[str]:
    """Return all string literals contained in one repository module."""

    tree = ast.parse((ROOT / relative).read_text(encoding="utf-8"))
    return {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }


def _class_definitions(relative: str, class_name: str) -> set[str]:
    """Return method names declared directly on one class."""

    tree = ast.parse((ROOT / relative).read_text(encoding="utf-8"))
    cls = next(
        node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == class_name
    )
    return {
        node.name for node in cls.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


def _class_annotated_fields(relative: str, class_name: str) -> tuple[str, ...]:
    """Return annotated field names declared directly on one class."""

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
    """Merge math and admission expose only their current named surfaces."""

    merge = _top_level_definitions("fs_diloco/protocol/merge.py")
    admission_fields = _class_annotated_fields("fs_diloco/protocol/authority.py", "Admission")

    assert merge == {
        "staleness",
        "raw_update_weight",
        "normalized_update_weights",
        "weighted_average_tensors",
    }
    assert admission_fields == ("fence", "resume")


def test_receipt_identity_and_paths_have_one_protocol_owner() -> None:
    """Receipt identity and path derivation have one protocol owner."""

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


def test_runtime_composition_has_one_current_actor_boundary() -> None:
    """Public learner and syncer entrypoints delegate to one runtime boundary."""

    learner = _top_level_definitions("fs_diloco/runtime/learner.py")
    syncer = _top_level_definitions("fs_diloco/runtime/syncer.py")
    learner_entrypoint = _top_level_definitions("fs_diloco/runtime/learner_entrypoint.py")
    syncer_entrypoint = _top_level_definitions("fs_diloco/runtime/syncer_entrypoint.py")
    public_learner_source = (ROOT / "fs_diloco/learner.py").read_text(encoding="utf-8")
    public_syncer_source = (ROOT / "fs_diloco/syncer.py").read_text(encoding="utf-8")

    assert "run_admitted_learner" in learner
    assert "run_fenced_syncer" in syncer
    assert {"build_parser", "main"} <= learner_entrypoint
    assert {"build_parser", "main"} <= syncer_entrypoint
    assert "__main__" not in _string_literals("fs_diloco/runtime/learner_entrypoint.py")
    assert "__main__" not in _string_literals("fs_diloco/runtime/syncer_entrypoint.py")
    assert "from .runtime.learner_entrypoint import main" in public_learner_source
    assert '"main"' in public_learner_source
    assert "from .runtime.syncer_entrypoint import main" in public_syncer_source
    assert '"main"' in public_syncer_source


def test_only_unversioned_product_surfaces_exist() -> None:
    """Tracked product paths contain no parallel generation-suffixed surfaces."""

    tracked = set(
        subprocess.check_output(
            ["git", "ls-files", "--", "fs_diloco", "configs", "scripts", "tests"],
            cwd=ROOT,
            text=True,
        ).splitlines()
    )
    assert tracked, "current source inventory is empty"
    generation_suffix = re.compile(r"(?:^|[/_.-])v[0-9]+(?:$|[/_.-])", re.IGNORECASE)
    assert not any(generation_suffix.search(path) for path in tracked)


def test_no_product_generation_suffix_in_tracked_contents() -> None:
    """Tracked product contents contain no current generation-suffixed naming."""

    tracked = tuple(
        subprocess.check_output(
            [
                "git",
                "ls-files",
                "--",
                "fs_diloco",
                "configs",
                "scripts/miyabi",
                "tests",
                "docs",
                "README.md",
                "pyproject.toml",
            ],
            cwd=ROOT,
            text=True,
        ).splitlines()
    )
    assert tracked, "current source inventory is empty"
    text_files = tuple(
        path
        for relative in tracked
        if (path := ROOT / relative).suffix
        in {".py", ".yaml", ".sql", ".sh", ".pbs", ".md", ".toml"}
    )
    assert text_files, "current text-source inventory is empty"
    product_generation = re.compile(
        r"(?i)\b(?:full[ _-]?protocol|fs[ _-]?diloco|protocol|schema|config)"
        r"[ _.-]*v[0-9]+\b|\bv[0-9]+[ _.-]*(?:full[ _-]?protocol|fs[ _-]?diloco|"
        r"legacy|protocol|schema|config)\b"
    )
    violations = {
        path.relative_to(ROOT).as_posix(): match.group(0)
        for path in text_files
        if (match := product_generation.search(path.read_text(encoding="utf-8")))
    }
    assert violations == {}

    versioned_wire_types: set[str] = set()
    for path in (ROOT / "fs_diloco").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        versioned_wire_types.update(
            node.name
            for node in ast.walk(tree)
            if isinstance(node, ast.ClassDef) and re.search(r"V[0-9]+$", node.name)
        )
    assert versioned_wire_types == {"CycleReceiptV1", "FullUpdateProposalV2"}


def test_python_floor_and_public_invocation_surface_are_unique() -> None:
    """The project declares one Python floor and one public invocation path."""

    metadata = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert metadata["project"]["requires-python"] == ">=3.13"
    assert metadata["tool"]["ruff"]["target-version"] == "py313"
    assert (ROOT / ".python-version").read_text(encoding="utf-8").strip() == "3.13"
    assert "scripts" not in metadata["project"]
    assert not (ROOT / "fs_diloco/cli.py").exists()
    protocol_init = ast.parse((ROOT / "fs_diloco/protocol/__init__.py").read_text(encoding="utf-8"))
    assert len(protocol_init.body) == 1 and isinstance(protocol_init.body[0], ast.Expr)


def test_artifact_versions_and_run_paths_have_explicit_owners() -> None:
    """Artifact formats and run paths retain explicit single owners."""

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
