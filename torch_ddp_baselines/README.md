# Standalone torch distributed baselines

This package is intentionally independent from `fs_diloco`. It exposes its own
training entrypoint, strict configuration, artifacts, health checker, and Miyabi
launch scripts. No module in this directory imports `fs_diloco`.

The two supported modes are:

- `ddp`: PyTorch DDP synchronizes gradients once per accumulated optimizer step.
- `periodic_average`: ranks train independently and average one flattened BF16
  parameter vector every 100 optimizer steps while retaining local AdamW state.

Run training through `python -m torch_ddp_baselines`. Validate a completed run
through `python -m torch_ddp_baselines.health`. On Miyabi, submit both pinned
GPT-2/WikiText-2 8-node, 500-step experiments with:

```bash
bash torch_ddp_baselines/scripts/miyabi/submit_500steps.sh
```

Each run refuses to overwrite prior evidence and publishes a manifest, resolved
config, per-rank metrics/logs/heartbeats, synchronization metrics, final
safetensors checkpoint, summary, and terminal health result.
