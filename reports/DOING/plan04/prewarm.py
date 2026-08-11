"""Prewarm the exact plan04 Hub inputs and execute one GPU training micro-step."""

from __future__ import annotations

import json
from pathlib import Path

import torch

from fs_diloco.core.config import load_config
from fs_diloco.modeling.hf_data import build_indexed_batch_iterator
from fs_diloco.modeling.hf_model import load_causal_lm_and_tokenizer
from fs_diloco.protocol.data_cursor import IndexedBlockCursor


def main() -> None:
    """Load pinned artifacts, materialize indexed data, and validate GPU forward/backward."""

    project_root = Path(__file__).resolve().parents[3]
    config = load_config(project_root / "configs/dynamic_full/gpt2_wikitext2_8l_200x10.yaml")
    model, tokenizer = load_causal_lm_and_tokenizer(config.model)
    cursor = IndexedBlockCursor(
        stable_contributor_key="0",
        dataset_identity_sha256="0" * 64,
        seed=config.training.seed,
        block_index=0,
        shard_index=0,
        shard_count=config.membership.stream_pool_size,
    )
    batch = next(build_indexed_batch_iterator(config, tokenizer, cursor=cursor))
    device = torch.device("cuda")
    model.to(device)
    output = model(
        input_ids=batch.input_ids.to(device),
        labels=batch.labels.to(device),
    )
    if output.loss is None or not torch.isfinite(output.loss):
        raise RuntimeError("exact plan04 micro-step produced no finite loss")
    output.loss.backward()
    torch.cuda.synchronize(device)
    print(
        json.dumps(
            {
                "status": "PASS",
                "device": str(device),
                "loss": float(output.loss.detach().float().cpu().item()),
                "tokens": batch.num_tokens,
                "model": config.model.name_or_path,
                "model_revision": config.model.revision,
                "dataset": config.data.dataset_name,
                "dataset_revision": config.data.revision,
            },
            sort_keys=True,
        ),
        flush=True,
    )


if __name__ == "__main__":
    main()
