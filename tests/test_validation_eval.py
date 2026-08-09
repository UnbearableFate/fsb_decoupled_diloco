import json
import math

import pytest
import torch

from fs_diloco.core.config import load_config, load_resolved_config_snapshot
from fs_diloco.legacy.config_v1_v3 import load_query_config_snapshot
from fs_diloco.tools.validation_eval import (
    attach_validation_to_summary,
    causal_cross_entropy_sum,
    evaluated_global_version,
    finalize_validation_metrics,
    resolve_terminal_predecessor_checkpoint,
    validate_checkpoint_identity,
)
from fs_diloco.storage.atomic_io import sha256_file


def test_causal_loss_uses_shift_and_predicted_token_weighting():
    logits = torch.zeros((2, 4, 5), dtype=torch.float32)
    input_ids = torch.tensor([[0, 1, 2, 3], [4, 3, 2, 1]], dtype=torch.long)

    loss_sum, predicted_tokens = causal_cross_entropy_sum(logits, input_ids)

    assert predicted_tokens == 6
    assert float(loss_sum) == pytest.approx(6 * math.log(5))


@pytest.mark.parametrize(
    ("loss_sum", "tokens", "blocks", "message"),
    [
        (0.0, 0, 1, "predicted tokens"),
        (0.0, 1, 0, "blocks"),
        (float("nan"), 1, 1, "non-finite"),
    ],
)
def test_validation_metrics_fail_closed(loss_sum, tokens, blocks, message):
    with pytest.raises(ValueError, match=message):
        finalize_validation_metrics(
            total_loss_sum=loss_sum,
            predicted_tokens=tokens,
            block_count=blocks,
        )


def test_validation_metrics_are_token_weighted_and_finite():
    result = finalize_validation_metrics(
        total_loss_sum=12.0,
        predicted_tokens=6,
        block_count=2,
    )
    assert result == {
        "validation_loss": 2.0,
        "validation_perplexity": pytest.approx(math.exp(2.0)),
        "predicted_tokens": 6,
        "block_count": 2,
    }


def test_checkpoint_identity_requires_latest_unless_explicitly_allowed(tmp_path):
    checkpoint = tmp_path / "global_v000001.safetensors"
    other = tmp_path / "global_v000002.safetensors"
    checkpoint.write_bytes(b"checkpoint")
    other.write_bytes(b"other")

    with pytest.raises(ValueError, match="does not match latest"):
        validate_checkpoint_identity(checkpoint, {"weight_path": str(other)})
    identity = validate_checkpoint_identity(
        checkpoint,
        {"weight_path": str(other)},
        allow_non_latest=True,
    )
    assert identity["checkpoint_size_bytes"] == len(b"checkpoint")
    assert identity["checkpoint_sha256"].startswith("sha256:")


def test_terminal_predecessor_resolution_selects_highest_version_and_checks_checksum(
    tmp_path,
):
    evidence = tmp_path / "eval_checkpoints"
    evidence.mkdir()
    for version in (7, 9):
        checkpoint = evidence / f"terminal_predecessor_v{version:06d}.safetensors"
        checkpoint.write_bytes(f"v{version}".encode())
        manifest = {
            "checkpoint_role": "terminal_predecessor_evidence",
            "source_global_version": version,
            "checkpoint_path": str(checkpoint.relative_to(tmp_path)),
            "checkpoint_sha256": f"sha256:{sha256_file(checkpoint)}",
        }
        (evidence / f"terminal_predecessor_v{version:06d}.manifest.json").write_text(
            json.dumps(manifest), encoding="utf-8"
        )

    checkpoint, manifest = resolve_terminal_predecessor_checkpoint(tmp_path)

    assert checkpoint.name == "terminal_predecessor_v000009.safetensors"
    assert manifest["source_global_version"] == 9
    checkpoint.write_bytes(b"corrupt")
    with pytest.raises(ValueError, match="checksum"):
        resolve_terminal_predecessor_checkpoint(tmp_path)


def test_terminal_predecessor_resolution_requires_capture_manifest(tmp_path):
    with pytest.raises(FileNotFoundError, match="manifest"):
        resolve_terminal_predecessor_checkpoint(tmp_path)


def test_evaluated_global_version_uses_terminal_predecessor_source_version():
    checkpoint_manifest = {"global_version": 53}
    predecessor_capture = {"source_global_version": 52}

    assert evaluated_global_version(checkpoint_manifest, None) == 53
    assert evaluated_global_version(checkpoint_manifest, predecessor_capture) == 52
    with pytest.raises(ValueError, match="valid global version"):
        evaluated_global_version(checkpoint_manifest, {})


def test_summary_attachment_is_atomic_idempotent_and_mismatch_safe(tmp_path):
    summary_path = tmp_path / "control" / "summary.json"
    result_path = tmp_path / "metrics" / "validation_eval.json"
    summary_path.parent.mkdir(parents=True)
    result_path.parent.mkdir(parents=True)
    summary_path.write_text(json.dumps({"run_id": "run", "final_version": 3}))
    result = {
        "status": "success",
        "checkpoint_sha256": "sha256:abc",
        "validation_loss": 2.0,
        "validation_perplexity": math.exp(2.0),
        "predicted_tokens": 10,
        "block_count": 2,
    }

    attach_validation_to_summary(summary_path, result_path, result)
    attach_validation_to_summary(summary_path, result_path, result)

    attached = json.loads(summary_path.read_text())["validation_eval"]
    assert attached["checkpoint_sha256"] == "sha256:abc"
    assert attached["result_path"] == str(result_path.resolve())
    with pytest.raises(ValueError, match="different checkpoint"):
        attach_validation_to_summary(
            summary_path,
            result_path,
            {**result, "checkpoint_sha256": "sha256:different"},
        )


def test_current_snapshot_loader_refuses_legacy_runtime_keys(tmp_path):
    snapshot = tmp_path / "resolved.yaml"
    snapshot.write_text(
        """
sync:
  upload_mode: filesystem
learner:
  prediction_reconcile_timeout_seconds: 12.5
""",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="sync.upload_mode"):
        load_config(snapshot)
    with pytest.raises(ValueError, match="sync.upload_mode"):
        load_resolved_config_snapshot(snapshot)

    projected = load_query_config_snapshot(snapshot)
    assert projected.sync.stop_after_outer_steps == 20
    assert projected.learner.prediction.reconcile_timeout_seconds == 12.5


def test_query_config_projection_supports_old_fragment_eval_without_runtime_modes(
    tmp_path,
):
    snapshot = tmp_path / "legacy-fragment.yaml"
    snapshot.write_text(
        """config_schema_version: 1
run:
  name: historical-fragment
model:
  name_or_path: gpt2
data:
  dataset_name: wikitext
  validation_split: validation
fragments:
  enabled: true
failure_sim:
  enabled: false
coordination:
  syncer_ha:
    enabled: true
sync:
  upload_mode: fragment
  stop_after_global_tokens: 1024
learner:
  prediction_reconcile_timeout_seconds: 17.0
""",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="移除|removed"):
        load_resolved_config_snapshot(snapshot)
    projected = load_query_config_snapshot(snapshot)

    assert projected.run.name == "historical-fragment"
    assert projected.model.name_or_path == "gpt2"
    assert projected.data.validation_split == "validation"
    assert projected.learner.prediction.reconcile_timeout_seconds == 17.0
