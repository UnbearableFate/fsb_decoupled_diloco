# Filesystem DiLoCo validation loss/perplexity protocol

## Frozen primary metric

- Configuration authority: the evaluated run's `control/run_config.resolved.yaml`.
- Dataset authority: `data.dataset_name`, `data.dataset_config_name`, and `data.validation_split` from that snapshot. The result records the resolved dataset fingerprint, builder version, and cache files.
- Tokenization: the configured model tokenizer; each non-empty text is encoded with `add_special_tokens=false`, followed by one EOS token when the tokenizer defines one.
- Blocking: concatenate the validation token stream, take non-overlapping blocks of `data.block_size`, and drop the incomplete tail. No training shuffle or learner sharding is applied.
- Causal loss: `logits[:, :-1]` predicts `input_ids[:, 1:]`. Sum cross-entropy over all predicted tokens in every block, then divide once by the total predicted-token count. Perplexity is `exp(validation_loss)`.
- Runtime defaults: batch size 4 and BF16 evaluation on CUDA when available. Any override is part of the protocol payload and changes `protocol_sha256`; controlled comparisons require identical protocol hashes.
- A successful result must have positive block/predicted-token counts and finite loss/perplexity.

## Identity and persistence

- Default evaluation accepts only the current `latest.json` weight. Q5 predecessor evidence uses `--terminal-predecessor`, which selects the highest verified capture manifest, checks its checksum, implies non-latest evaluation, writes a versioned result, and never replaces the terminal validation attachment in summary.
- The result records checkpoint absolute path, byte size, SHA-256, global version, param index, training source identity, and evaluator source identity. Formal runs require both identities; legacy observations must explicitly waive missing identity and cannot become causal comparisons.
- The authoritative result is atomically written to `metrics/validation_eval.json`; only after it is complete is the same checkpoint-tagged result atomically attached under `validation_eval` in `control/summary.json`.
- A summary already attached to a different checkpoint fails closed. A failed/empty eval never writes a success attachment.

`lm-eval-harness` remains an optional downstream-task extension. Its WikiText task is not the primary metric because it does not by itself prove equivalence to this configured split/tokenization/block protocol.
