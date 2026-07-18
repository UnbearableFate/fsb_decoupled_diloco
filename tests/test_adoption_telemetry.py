import json
import time
import torch

from fs_diloco.core.config import resolve_config
from fs_diloco.modeling.hf_model import TinyCausalLM
from fs_diloco.observability.metrics import LEARNER_METRIC_FIELDS
from fs_diloco.runtime.adoption import AdoptionOutcome, StrategyAction
from fs_diloco.runtime.learner import apply_fragment_adoption, finalize_strategy_action
from fs_diloco.tools.analysis import summarize_run


class RecordingLogger:
    def __init__(self):
        self.events = []

    def event(self, event_type, **fields):
        self.events.append({"event_type": event_type, **fields})


def test_full_adoption_event_splits_load_apply_and_optimizer_reset_time():
    config = resolve_config("configs/fs_diloco_tiny_local.yaml")
    model = TinyCausalLM(vocab_size=8, hidden_size=4)
    optimizer = torch.optim.AdamW(model.parameters(), lr=1e-3)
    logger = RecordingLogger()
    action = StrategyAction(
        adoption=AdoptionOutcome(
            version=2,
            latest={"version": 2},
            tokens_since_global_load=0,
            preserve_inner_state=False,
            reason="global_adopted",
            load_apply_seconds=0.25,
        )
    )

    finalize_strategy_action(
        action,
        model=model,
        config=config,
        logger=logger,
        current_version=1,
        optimizer=optimizer,
        scheduler=None,
        completed_local_steps=4,
    )

    adopted = next(event for event in logger.events if event["event_type"] == "global_adopted")
    assert adopted["adoption_load_apply_seconds"] == 0.25
    assert adopted["adoption_optimizer_reset_seconds"] >= 0.0
    assert adopted["adoption_pause_seconds"] == (
        adopted["adoption_load_apply_seconds"]
        + adopted["adoption_optimizer_reset_seconds"]
    )


def test_fragment_adoption_event_excludes_wait_before_helper_and_splits_reset_time():
    config = resolve_config("configs/fs_diloco_tiny_fragment_local.yaml")
    model = TinyCausalLM(vocab_size=8, hidden_size=4)
    optimizer = torch.optim.AdamW(model.parameters(), lr=1e-3)
    logger = RecordingLogger()

    def adopt_fn(**_kwargs):
        time.sleep(0.001)
        return 7, {0: 2}, [0]

    apply_fragment_adoption(
        model=model,
        latest={"global_merge_event": 7},
        param_index={},
        fragment_index={},
        last_loaded_fragment_versions={0: 1},
        tokens_since_fragment_load={0: 10},
        fragment_adopt_count=0,
        last_adopted_fragments=[],
        optimizer=optimizer,
        scheduler=None,
        device=torch.device("cpu"),
        config=config,
        logger=logger,
        event_type="fragments_adopted",
        reset_tokens=True,
        reset_optimizer=True,
        include_fragment_versions=True,
        completed_local_steps=4,
        adopt_fn=adopt_fn,
    )

    adopted = next(event for event in logger.events if event["event_type"] == "fragments_adopted")
    assert adopted["adoption_load_apply_seconds"] >= 0.001
    assert adopted["adoption_optimizer_reset_seconds"] >= 0.0
    assert adopted["adoption_pause_seconds"] == (
        adopted["adoption_load_apply_seconds"]
        + adopted["adoption_optimizer_reset_seconds"]
    )


def test_analysis_aggregates_adoption_pause_over_completed_cycle_elapsed(tmp_path):
    metrics = tmp_path / "metrics"
    logs = tmp_path / "logs"
    metrics.mkdir()
    logs.mkdir()
    (metrics / "learner_metrics.csv").write_text(
        "learner_id,local_cycle_elapsed_seconds\n"
        "learner_000,2.0\n"
        "learner_000,3.0\n"
        "learner_001,4.0\n",
        encoding="utf-8",
    )
    events = [
        {"event_type": "global_adopted", "adoption_pause_seconds": 0.4},
        {"event_type": "global_adopted", "adoption_pause_seconds": 0.6},
    ]
    (logs / "learner_000.jsonl").write_text(
        "".join(json.dumps(event) + "\n" for event in events),
        encoding="utf-8",
    )
    (logs / "learner_001.jsonl").write_text(
        json.dumps({"event_type": "global_adopted", "version": 1}) + "\n",
        encoding="utf-8",
    )

    summary = summarize_run(tmp_path)["learner_adoption_pause"]

    assert summary["learner_000"] == {
        "status": "available",
        "adoption_count": 2,
        "adoption_pause_total_seconds": 1.0,
        "adoption_pause_mean_seconds": 0.5,
        "completed_cycle_elapsed_seconds": 5.0,
        "adoption_pause_fraction": 0.2,
    }
    assert summary["learner_001"]["status"] == "unavailable"
    assert summary["learner_001"]["adoption_pause_fraction"] is None
    assert "local_cycle_elapsed_seconds" in LEARNER_METRIC_FIELDS
