"""Evaluate a run checkpoint on its configured validation split."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import time
from pathlib import Path
from typing import Any

import torch
import torch.nn.functional as F

from ..legacy.config_v1_v3 import load_query_config_snapshot
from ..modeling.hf_data import load_text_split, text_rows_to_blocks
from ..modeling.hf_model import load_causal_lm_and_tokenizer
from ..modeling.param_index import build_param_index, load_param_index, validate_compatible_index
from ..storage.atomic_io import atomic_write_json, safe_read_json, sha256_file
from ..storage.tensor_codec import dtype_from_name, load_global_weights_into_model
from .eval_lm_harness import resolve_checkpoint


def causal_cross_entropy_sum(
    logits: torch.Tensor,
    input_ids: torch.Tensor,
) -> tuple[torch.Tensor, int]:
    if logits.ndim != 3 or input_ids.ndim != 2:
        raise ValueError(
            "causal validation expects logits [batch, seq, vocab] and ids [batch, seq]"
        )
    if logits.shape[:2] != input_ids.shape or input_ids.shape[1] < 2:
        raise ValueError("logit/input shape mismatch or sequence too short for causal shift")
    shift_logits = logits[:, :-1, :].contiguous().float()
    shift_labels = input_ids[:, 1:].contiguous()
    predicted_tokens = int(shift_labels.numel())
    loss_sum = F.cross_entropy(
        shift_logits.reshape(-1, shift_logits.shape[-1]),
        shift_labels.reshape(-1),
        reduction="sum",
    )
    return loss_sum, predicted_tokens


def finalize_validation_metrics(
    *,
    total_loss_sum: float,
    predicted_tokens: int,
    block_count: int,
) -> dict[str, float | int]:
    if block_count <= 0:
        raise ValueError("validation produced zero blocks")
    if predicted_tokens <= 0:
        raise ValueError("validation produced zero predicted tokens")
    if not math.isfinite(float(total_loss_sum)):
        raise ValueError("validation produced non-finite loss sum")
    loss = float(total_loss_sum) / int(predicted_tokens)
    perplexity = math.exp(loss)
    if not math.isfinite(loss) or not math.isfinite(perplexity):
        raise ValueError("validation produced non-finite loss/perplexity")
    return {
        "validation_loss": loss,
        "validation_perplexity": perplexity,
        "predicted_tokens": int(predicted_tokens),
        "block_count": int(block_count),
    }


def validate_checkpoint_identity(
    checkpoint_path: str | Path,
    latest: dict[str, Any],
    *,
    allow_non_latest: bool = False,
) -> dict[str, Any]:
    checkpoint = Path(checkpoint_path).expanduser().resolve()
    if not checkpoint.is_file():
        raise FileNotFoundError(f"checkpoint does not exist: {checkpoint}")
    latest_weight = latest.get("weight_path")
    if not allow_non_latest:
        if not latest_weight:
            raise ValueError("latest.json does not define weight_path")
        if checkpoint != Path(str(latest_weight)).expanduser().resolve():
            raise ValueError(
                f"checkpoint {checkpoint} does not match latest weight {latest_weight}"
            )
    return {
        "checkpoint_path": str(checkpoint),
        "checkpoint_size_bytes": checkpoint.stat().st_size,
        "checkpoint_sha256": f"sha256:{sha256_file(checkpoint)}",
    }


def resolve_terminal_predecessor_checkpoint(
    run_root: str | Path,
) -> tuple[Path, dict[str, Any]]:
    """Resolve and verify the newest captured predecessor evidence checkpoint."""
    root = Path(run_root).resolve()
    candidates: list[tuple[int, Path, dict[str, Any]]] = []
    for manifest_path in sorted((root / "eval_checkpoints").glob("*.manifest.json")):
        manifest = safe_read_json(manifest_path)
        if not manifest or manifest.get("checkpoint_role") != "terminal_predecessor_evidence":
            continue
        try:
            version = int(manifest["source_global_version"])
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError(
                f"invalid terminal predecessor manifest version: {manifest_path}"
            ) from error
        candidates.append((version, manifest_path, manifest))
    if not candidates:
        raise FileNotFoundError(
            f"no terminal predecessor capture manifest found under {root / 'eval_checkpoints'}"
        )
    _version, manifest_path, manifest = max(candidates, key=lambda item: item[0])
    relative_checkpoint = manifest.get("checkpoint_path")
    if not relative_checkpoint:
        raise ValueError(f"capture manifest is missing checkpoint_path: {manifest_path}")
    checkpoint = (root / str(relative_checkpoint)).resolve()
    if not checkpoint.is_relative_to(root) or not checkpoint.is_file():
        raise ValueError(f"capture manifest checkpoint is missing or outside run: {checkpoint}")
    actual_sha256 = f"sha256:{sha256_file(checkpoint)}"
    if manifest.get("checkpoint_sha256") != actual_sha256:
        raise ValueError(f"terminal predecessor capture checksum mismatch: {checkpoint}")
    return checkpoint, manifest


def evaluated_global_version(
    checkpoint_manifest: dict[str, Any],
    terminal_predecessor_capture: dict[str, Any] | None,
) -> int:
    """Return the version represented by the checkpoint being evaluated."""
    if terminal_predecessor_capture is not None:
        value = terminal_predecessor_capture.get("source_global_version")
        context = "terminal predecessor capture"
    else:
        value = checkpoint_manifest.get("global_version")
        context = "checkpoint manifest"
    try:
        version = int(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{context} is missing a valid global version") from error
    if version < 0:
        raise ValueError(f"{context} global version must be nonnegative")
    return version


def attach_validation_to_summary(
    summary_path: str | Path,
    result_path: str | Path,
    result: dict[str, Any],
) -> None:
    summary_file = Path(summary_path).resolve()
    result_file = Path(result_path).resolve()
    summary = safe_read_json(summary_file)
    if not summary:
        raise FileNotFoundError(f"completed run summary is missing or unreadable: {summary_file}")
    checkpoint_sha256 = result.get("checkpoint_sha256")
    if not checkpoint_sha256:
        raise ValueError("validation result is missing checkpoint_sha256")
    existing = summary.get("validation_eval")
    if isinstance(existing, dict) and existing.get("checkpoint_sha256") != checkpoint_sha256:
        raise ValueError("summary already contains validation for a different checkpoint")
    attached = {**result, "result_path": str(result_file)}
    atomic_write_json(result_file, result)
    summary["validation_eval"] = attached
    atomic_write_json(summary_file, summary)


def _dataset_identity(dataset: Any, data_config: Any, split: str) -> dict[str, Any]:
    info = getattr(dataset, "info", None)
    cache_files = getattr(dataset, "cache_files", None) or []
    return {
        "dataset_name": data_config.dataset_name,
        "dataset_config_name": data_config.dataset_config_name,
        "validation_split": split,
        "streaming": bool(data_config.streaming),
        "dataset_fingerprint": getattr(dataset, "_fingerprint", None),
        "dataset_builder_name": getattr(info, "builder_name", None),
        "dataset_version": str(getattr(info, "version", "")) or None,
        "cache_files": [str(item.get("filename")) for item in cache_files if item.get("filename")],
    }


def _protocol_hash(protocol: dict[str, Any]) -> str:
    encoded = json.dumps(protocol, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def _source_identity(run_root: Path, *, allow_missing: bool) -> dict[str, Any]:
    training = safe_read_json(run_root / "control" / "source_identity.json") or {}
    evaluator = {
        "git_commit": os.environ.get("FS_DILOCO_GIT_COMMIT"),
        "git_dirty": os.environ.get("FS_DILOCO_GIT_DIRTY"),
        "source_fingerprint": os.environ.get("FS_DILOCO_SOURCE_FINGERPRINT"),
    }
    training_complete = bool(training.get("git_commit") and training.get("source_fingerprint"))
    evaluator_complete = bool(evaluator["git_commit"] and evaluator["source_fingerprint"])
    if not allow_missing and (not training_complete or not evaluator_complete):
        raise ValueError("validation requires complete training and evaluator source identity")
    return {
        "training": training or None,
        "training_status": "available" if training_complete else "unavailable_legacy",
        "evaluator": evaluator if evaluator_complete else None,
        "evaluator_status": "available" if evaluator_complete else "unavailable",
    }


@torch.no_grad()
def evaluate_blocks(
    model: torch.nn.Module,
    blocks: list[list[int]],
    *,
    batch_size: int,
    device: torch.device,
) -> dict[str, float | int]:
    if batch_size <= 0:
        raise ValueError("batch_size must be positive")
    total_loss_sum = 0.0
    predicted_tokens = 0
    for start in range(0, len(blocks), batch_size):
        input_ids = torch.tensor(blocks[start : start + batch_size], dtype=torch.long)
        input_ids = input_ids.to(device)
        outputs = model(input_ids=input_ids)
        loss_sum, batch_predicted_tokens = causal_cross_entropy_sum(outputs.logits, input_ids)
        total_loss_sum += float(loss_sum.item())
        predicted_tokens += batch_predicted_tokens
    return finalize_validation_metrics(
        total_loss_sum=total_loss_sum,
        predicted_tokens=predicted_tokens,
        block_count=len(blocks),
    )


def run_validation(args: argparse.Namespace) -> dict[str, Any]:
    project_root = args.project_root.resolve()
    terminal_predecessor_capture: dict[str, Any] | None = None
    checkpoint_argument = args.checkpoint
    if args.terminal_predecessor:
        checkpoint_argument, terminal_predecessor_capture = resolve_terminal_predecessor_checkpoint(
            args.run_root
        )
    manifest = resolve_checkpoint(
        project_root=project_root,
        checkpoint=str(checkpoint_argument) if checkpoint_argument else None,
        run_root=str(args.run_root) if args.run_root else None,
        config=str(args.config) if args.config else None,
    )
    run_root = Path(manifest["source_run_root"])
    latest = safe_read_json(run_root / "control" / "latest.json") or {}
    checkpoint_identity = validate_checkpoint_identity(
        manifest["checkpoint_path"],
        latest,
        allow_non_latest=args.allow_non_latest or args.terminal_predecessor,
    )
    source_identity = _source_identity(
        run_root,
        allow_missing=args.allow_missing_source_identity,
    )

    config = load_query_config_snapshot(manifest["config_path"])
    training_identity = source_identity.get("training") or {}
    configured_fingerprint = config.run.source_fingerprint
    captured_fingerprint = training_identity.get("source_fingerprint")
    if configured_fingerprint and captured_fingerprint != configured_fingerprint:
        raise ValueError(
            "resolved config source_fingerprint does not match captured training identity"
        )
    if config.data.dataset_name == "synthetic":
        raise ValueError("formal validation evaluator requires a configured text dataset")
    dataset = load_text_split(config.data, config.data.validation_split)
    model, tokenizer = load_causal_lm_and_tokenizer(config.model)
    blocks = text_rows_to_blocks(dataset, tokenizer, int(config.data.block_size))
    if args.max_blocks is not None:
        blocks = blocks[: int(args.max_blocks)]

    device_name = (
        ("cuda" if torch.cuda.is_available() else "cpu") if args.device == "auto" else args.device
    )
    device = torch.device(device_name)
    eval_dtype = dtype_from_name(args.dtype)
    model.to(device=device, dtype=eval_dtype)
    param_index = load_param_index(manifest["param_index_path"])
    current_index = build_param_index(model, model_name_or_path=config.model.name_or_path)
    validate_compatible_index(current_index, param_index)
    load_global_weights_into_model(
        manifest["checkpoint_path"],
        model,
        param_index,
        strict_shape=True,
    )
    model.eval()
    metrics = evaluate_blocks(
        model,
        blocks,
        batch_size=args.batch_size,
        device=device,
    )

    protocol = {
        "protocol_version": 1,
        "validation_split": config.data.validation_split,
        "tokenizer_class": type(tokenizer).__name__,
        "tokenizer_name_or_path": getattr(tokenizer, "name_or_path", None),
        "tokenizer_vocab_size": getattr(tokenizer, "vocab_size", None),
        "add_special_tokens": False,
        "append_eos_per_nonempty_text": getattr(tokenizer, "eos_token_id", None) is not None,
        "eos_token_id": getattr(tokenizer, "eos_token_id", None),
        "block_size": int(config.data.block_size),
        "non_overlapping_blocks": True,
        "drop_incomplete_tail": True,
        "causal_shift": "logits[:, :-1] -> input_ids[:, 1:]",
        "loss_reduction": "sum_over_predicted_tokens_then_divide",
        "batch_size": int(args.batch_size),
        "dtype": args.dtype,
        "device": str(device),
        "max_blocks": args.max_blocks,
    }
    result = {
        "format_version": 1,
        "status": "success",
        "evaluated_at": time.time(),
        "source_run_id": manifest["source_run_id"],
        "global_version": evaluated_global_version(
            manifest,
            terminal_predecessor_capture,
        ),
        **checkpoint_identity,
        "param_index_path": manifest["param_index_path"],
        "config_path": manifest["config_path"],
        "model_name_or_path": config.model.name_or_path,
        "dataset": _dataset_identity(dataset, config.data, config.data.validation_split),
        "protocol": protocol,
        "protocol_sha256": _protocol_hash(protocol),
        "source_identity": source_identity,
        "terminal_predecessor_capture": terminal_predecessor_capture,
        **metrics,
    }
    if args.output:
        output = args.output
    elif args.terminal_predecessor:
        version = int(terminal_predecessor_capture["source_global_version"])
        output = run_root / "metrics" / f"validation_terminal_predecessor_v{version:06d}.json"
    else:
        output = run_root / "metrics" / "validation_eval.json"
    if not args.no_attach_summary and not args.terminal_predecessor:
        attach_validation_to_summary(
            run_root / "control" / "summary.json",
            output,
            result,
        )
    else:
        atomic_write_json(output, result)
    return result


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", type=Path, default=Path.cwd())
    parser.add_argument("--run-root", type=Path)
    parser.add_argument("--checkpoint", type=Path)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--device", choices=("auto", "cpu", "cuda"), default="auto")
    parser.add_argument("--dtype", choices=("float32", "bfloat16"), default="bfloat16")
    parser.add_argument("--batch-size", type=int, default=4)
    parser.add_argument("--max-blocks", type=int)
    parser.add_argument("--allow-non-latest", action="store_true")
    parser.add_argument("--terminal-predecessor", action="store_true")
    parser.add_argument("--allow-missing-source-identity", action="store_true")
    parser.add_argument("--no-attach-summary", action="store_true")
    args = parser.parse_args(argv)
    if args.run_root is None and args.checkpoint is None:
        parser.error("one of --run-root or --checkpoint is required")
    if args.terminal_predecessor and (args.run_root is None or args.checkpoint is not None):
        parser.error("--terminal-predecessor requires --run-root and forbids --checkpoint")
    if args.batch_size <= 0:
        parser.error("--batch-size must be positive")
    if args.max_blocks is not None and args.max_blocks <= 0:
        parser.error("--max-blocks must be positive")
    return args


def main(argv: list[str] | None = None) -> None:
    result = run_validation(parse_args(argv))
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
