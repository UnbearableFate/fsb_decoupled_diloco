"""Export FS DiLoCo checkpoints for LM Evaluation Harness and flatten results."""

from __future__ import annotations

import argparse
import csv
import json
import re
import time
from pathlib import Path
from typing import Any


DEFAULT_SOURCE_RUN_ID = "20260709_142811_fs_diloco_gpt2_wikitext2_8l_5000steps"
DEFAULT_CHECKPOINT_RELATIVE = (
    f"runs/fs_diloco/{DEFAULT_SOURCE_RUN_ID}/weights/global_v000047.safetensors"
)
DEFAULT_CONFIG_RELATIVE = "configs/fs_diloco_gpt2_wikitext2_8l_5000steps.yaml"

_GLOBAL_WEIGHT_RE = re.compile(r"^global_v(\d{6})\.safetensors$")
_STDERR_SUFFIX = "_stderr"


def _read_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return payload


def _coerce_path(value: str | Path, *, base: Path | None = None) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute() and base is not None:
        path = base / path
    return path.resolve()


def _checkpoint_version(path: Path) -> int | None:
    match = _GLOBAL_WEIGHT_RE.match(path.name)
    if match is None:
        return None
    return int(match.group(1))


def _infer_run_root_from_checkpoint(checkpoint: Path) -> Path | None:
    if checkpoint.parent.name != "weights":
        return None
    return checkpoint.parent.parent


def _find_latest_run_root(project_root: Path) -> Path:
    candidates: list[tuple[float, Path]] = []
    for latest_path in (project_root / "runs" / "fs_diloco").glob("*/control/latest.json"):
        try:
            latest = _read_json(latest_path)
        except (OSError, json.JSONDecodeError, ValueError):
            continue
        weight_path = latest.get("weight_path")
        if weight_path and not _coerce_path(str(weight_path), base=project_root).exists():
            continue
        timestamp = latest.get("created_at")
        try:
            sort_key = float(timestamp)
        except (TypeError, ValueError):
            sort_key = latest_path.stat().st_mtime
        candidates.append((sort_key, latest_path.parent.parent))
    if not candidates:
        raise FileNotFoundError(
            f"no usable latest.json found below {project_root / 'runs/fs_diloco'}"
        )
    return max(candidates, key=lambda item: item[0])[1].resolve()


def resolve_checkpoint(
    *,
    project_root: Path,
    checkpoint: str | None = None,
    run_root: str | None = None,
    config: str | None = None,
) -> dict[str, Any]:
    """Resolve checkpoint, run metadata, config, and param index paths."""

    project_root = project_root.resolve()
    resolution_mode = "latest-run"

    checkpoint_path: Path | None = None
    if checkpoint:
        checkpoint_path = _coerce_path(checkpoint, base=project_root)
        resolution_mode = "explicit-checkpoint"

    if run_root:
        source_run_root = _coerce_path(run_root, base=project_root)
        if checkpoint_path is None:
            resolution_mode = "explicit-run-root"
    elif checkpoint_path is not None:
        source_run_root = _infer_run_root_from_checkpoint(checkpoint_path)
        if source_run_root is None:
            raise ValueError(
                "--run-root is required when --checkpoint is not under a weights/ directory"
            )
    else:
        source_run_root = _find_latest_run_root(project_root)

    latest_path = source_run_root / "control" / "latest.json"
    latest: dict[str, Any] = {}
    if latest_path.exists():
        latest = _read_json(latest_path)

    if checkpoint_path is None:
        latest_weight = latest.get("weight_path")
        if not latest_weight:
            raise ValueError(f"{latest_path} does not define weight_path")
        checkpoint_path = _coerce_path(str(latest_weight), base=project_root)

    if not checkpoint_path.exists():
        raise FileNotFoundError(f"checkpoint does not exist: {checkpoint_path}")

    global_version = _checkpoint_version(checkpoint_path)
    if global_version is None and latest.get("version") is not None:
        global_version = int(latest["version"])

    latest_version = None
    if latest.get("version") is not None:
        latest_version = int(latest["version"])
    total_seen_tokens = (
        latest.get("total_seen_tokens") if latest_version == global_version else None
    )

    param_index_value = latest.get("param_index_path")
    param_index_path = (
        _coerce_path(str(param_index_value), base=project_root)
        if param_index_value
        else source_run_root / "control" / "param_index.json"
    )
    if not param_index_path.exists():
        raise FileNotFoundError(f"param index does not exist: {param_index_path}")

    if config:
        config_path = _coerce_path(config, base=project_root)
    else:
        resolved_config = source_run_root / "control" / "run_config.resolved.yaml"
        config_path = (
            resolved_config if resolved_config.exists() else project_root / DEFAULT_CONFIG_RELATIVE
        )
    if not config_path.exists():
        raise FileNotFoundError(f"config does not exist: {config_path}")

    return {
        "project_root": str(project_root),
        "resolution_mode": resolution_mode,
        "source_run_root": str(source_run_root.resolve()),
        "source_run_id": str(latest.get("run_id") or source_run_root.name),
        "latest_json": str(latest_path.resolve()) if latest_path.exists() else None,
        "checkpoint_path": str(checkpoint_path.resolve()),
        "global_version": global_version,
        "total_seen_tokens": total_seen_tokens,
        "param_index_path": str(param_index_path.resolve()),
        "config_path": str(config_path.resolve()),
    }


def validate_query_manifest_output(
    manifest: dict[str, Any], output_path: str | Path, *, label: str
) -> tuple[Path, str]:
    """Classify the source and keep legacy-derived manifests outside its run root."""

    from ..legacy.reader import query_run_protocol, validate_query_output_path

    source_run_root = Path(str(manifest["source_run_root"])).resolve(strict=True)
    source_protocol = query_run_protocol(source_run_root)
    output = validate_query_output_path(
        source_run_root,
        output_path,
        source_protocol=source_protocol,
        label=label,
    )
    return output, source_protocol


def export_checkpoint(
    *,
    project_root: Path,
    export_dir: Path,
    eval_id: str,
    checkpoint: str | None = None,
    run_root: str | None = None,
    config: str | None = None,
    manifest_output: Path | None = None,
) -> dict[str, Any]:
    """Export an FS DiLoCo global checkpoint as a HuggingFace model directory."""

    from ..legacy.config_v1_v3 import load_query_config_snapshot
    from ..modeling.hf_model import load_causal_lm_and_tokenizer
    from ..modeling.param_index import (
        build_param_index,
        load_flat_into_model,
        load_param_index,
        validate_compatible_index,
    )
    from ..storage.atomic_io import atomic_write_json, ensure_dir
    from ..storage.tensor_codec import load_global_weights_flat

    project_root = project_root.resolve()
    export_dir = _coerce_path(export_dir, base=project_root)
    manifest = resolve_checkpoint(
        project_root=project_root,
        checkpoint=checkpoint,
        run_root=run_root,
        config=config,
    )
    export_dir, source_protocol = validate_query_manifest_output(
        manifest,
        export_dir,
        label="model export",
    )
    manifest["eval_id"] = eval_id
    manifest["export_dir"] = str(export_dir)
    manifest["source_protocol"] = source_protocol

    config_obj = load_query_config_snapshot(manifest["config_path"])
    model, tokenizer = load_causal_lm_and_tokenizer(config_obj.model)

    param_index = load_param_index(manifest["param_index_path"])
    current_index = build_param_index(model, model_name_or_path=config_obj.model.name_or_path)
    validate_compatible_index(current_index, param_index)

    flat = load_global_weights_flat(manifest["checkpoint_path"], param_index)
    load_flat_into_model(model, flat, param_index, strict_shape=True)
    if hasattr(model, "tie_weights"):
        model.tie_weights()

    ensure_dir(export_dir)
    model.save_pretrained(export_dir, safe_serialization=True)
    if hasattr(tokenizer, "save_pretrained"):
        tokenizer.save_pretrained(export_dir)

    manifest["model_name_or_path"] = config_obj.model.name_or_path
    manifest["exported_at"] = time.time()
    if manifest_output is not None:
        manifest_path = _coerce_path(manifest_output, base=project_root)
        manifest_path, manifest_source_protocol = validate_query_manifest_output(
            manifest,
            manifest_path,
            label="export manifest",
        )
        if manifest_source_protocol != source_protocol:
            raise RuntimeError("source protocol classification changed during model export")
        manifest["manifest_path"] = str(manifest_path)
        atomic_write_json(manifest_path, manifest)
    return manifest


def _metric_stderr_base(metric_name: str) -> str | None:
    if "," in metric_name:
        head, tail = metric_name.split(",", 1)
        if head.endswith(_STDERR_SUFFIX):
            return f"{head[: -len(_STDERR_SUFFIX)]},{tail}"
        return None
    if metric_name.endswith(_STDERR_SUFFIX):
        return metric_name[: -len(_STDERR_SUFFIX)]
    return None


def _as_float(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


def _result_json_files(path: Path) -> list[Path]:
    if path.is_file():
        return [path]
    files = sorted(path.rglob("results_*.json"))
    if not files:
        files = sorted(path.rglob("results.json"))
    return files


def results_to_csv(
    *,
    lm_eval_output: Path,
    output_csv: Path,
    eval_id: str | None = None,
    manifest: Path | None = None,
) -> list[dict[str, Any]]:
    """Flatten lm-eval results JSON files into a metrics CSV."""

    from ..storage.atomic_io import ensure_dir

    lm_eval_output = lm_eval_output.resolve()
    output_csv = output_csv.resolve()
    manifest_payload: dict[str, Any] = {}
    if manifest is not None and manifest.exists():
        manifest_payload = _read_json(manifest)
        source_run_root = manifest_payload.get("source_run_root")
        declared_protocol = manifest_payload.get("source_protocol")
        if isinstance(source_run_root, str):
            from ..legacy.reader import query_run_protocol, validate_query_output_path

            source_protocol = query_run_protocol(source_run_root)
            if declared_protocol is not None and declared_protocol != source_protocol:
                raise ValueError("evaluation manifest source protocol does not match authority")

            output_csv = validate_query_output_path(
                source_run_root,
                output_csv,
                source_protocol=source_protocol,
                label="evaluation CSV",
            )

    rows: list[dict[str, Any]] = []
    for json_path in _result_json_files(lm_eval_output):
        payload = _read_json(json_path)
        results = payload.get("results")
        if not isinstance(results, dict):
            continue
        for task_name, task_metrics in results.items():
            if not isinstance(task_metrics, dict):
                continue
            stderr_by_metric: dict[str, float] = {}
            for metric_name, value in task_metrics.items():
                if not isinstance(metric_name, str):
                    continue
                base_metric = _metric_stderr_base(metric_name)
                stderr = _as_float(value)
                if base_metric is not None and stderr is not None:
                    stderr_by_metric[base_metric] = stderr

            for metric_name, value in task_metrics.items():
                if not isinstance(metric_name, str) or _metric_stderr_base(metric_name) is not None:
                    continue
                metric_value = _as_float(value)
                if metric_value is None:
                    continue
                rows.append(
                    {
                        "eval_id": eval_id
                        or manifest_payload.get("eval_id")
                        or lm_eval_output.name,
                        "source_run_id": manifest_payload.get("source_run_id"),
                        "global_version": manifest_payload.get("global_version"),
                        "total_seen_tokens": manifest_payload.get("total_seen_tokens"),
                        "checkpoint_path": manifest_payload.get("checkpoint_path"),
                        "task": task_name,
                        "metric": metric_name,
                        "value": metric_value,
                        "stderr": stderr_by_metric.get(metric_name),
                        "lm_eval_json": str(json_path),
                    }
                )

    if not rows:
        raise ValueError(f"no numeric lm-eval metrics found below {lm_eval_output}")

    ensure_dir(output_csv.parent)
    fields = [
        "eval_id",
        "source_run_id",
        "global_version",
        "total_seen_tokens",
        "checkpoint_path",
        "task",
        "metric",
        "value",
        "stderr",
        "lm_eval_json",
    ]
    with output_csv.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    return rows


def _print_json(payload: dict[str, Any] | list[dict[str, Any]]) -> None:
    print(json.dumps(payload, sort_keys=True, indent=2))


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    resolve_parser = subparsers.add_parser("resolve-checkpoint")
    resolve_parser.add_argument("--project-root", default=".")
    resolve_parser.add_argument("--checkpoint")
    resolve_parser.add_argument("--run-root")
    resolve_parser.add_argument("--config")
    resolve_parser.add_argument("--manifest-output")

    export_parser = subparsers.add_parser("export-checkpoint")
    export_parser.add_argument("--project-root", default=".")
    export_parser.add_argument("--checkpoint")
    export_parser.add_argument("--run-root")
    export_parser.add_argument("--config")
    export_parser.add_argument("--eval-id", required=True)
    export_parser.add_argument("--export-dir", required=True)
    export_parser.add_argument("--manifest-output", required=True)

    csv_parser = subparsers.add_parser("results-to-csv")
    csv_parser.add_argument("--lm-eval-output", required=True)
    csv_parser.add_argument("--output-csv", required=True)
    csv_parser.add_argument("--eval-id")
    csv_parser.add_argument("--manifest")

    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    if args.command == "resolve-checkpoint":
        manifest = resolve_checkpoint(
            project_root=_coerce_path(args.project_root),
            checkpoint=args.checkpoint,
            run_root=args.run_root,
            config=args.config,
        )
        if args.manifest_output:
            from ..storage.atomic_io import atomic_write_json

            output_path, source_protocol = validate_query_manifest_output(
                manifest,
                _coerce_path(args.manifest_output, base=Path.cwd()),
                label="checkpoint manifest",
            )
            manifest["source_protocol"] = source_protocol
            manifest["manifest_path"] = str(output_path)
            atomic_write_json(output_path, manifest)
        _print_json(manifest)
    elif args.command == "export-checkpoint":
        manifest = export_checkpoint(
            project_root=_coerce_path(args.project_root),
            export_dir=Path(args.export_dir),
            eval_id=args.eval_id,
            checkpoint=args.checkpoint,
            run_root=args.run_root,
            config=args.config,
            manifest_output=Path(args.manifest_output),
        )
        _print_json(manifest)
    elif args.command == "results-to-csv":
        rows = results_to_csv(
            lm_eval_output=Path(args.lm_eval_output),
            output_csv=Path(args.output_csv),
            eval_id=args.eval_id,
            manifest=Path(args.manifest) if args.manifest else None,
        )
        _print_json({"rows": len(rows), "output_csv": str(Path(args.output_csv).resolve())})
    else:
        raise AssertionError(f"unhandled command: {args.command}")


if __name__ == "__main__":
    main()
