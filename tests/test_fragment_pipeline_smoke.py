from pathlib import Path


def test_fragment_pipeline_artifacts_are_present():
    root = Path(__file__).resolve().parents[1]
    for path in [
        root / "configs" / "fs_diloco_tiny_fragment_local.yaml",
        root / "configs" / "fs_diloco_gpt2_wikitext2_1l_fragment_debug.yaml",
        root / "configs" / "fs_diloco_gpt2_wikitext2_8l_fragment_50x4.yaml",
        root / "configs" / "fs_diloco_gpt2_wikitext2_8l_fragment_5000steps.yaml",
        root / "scripts" / "miyabi" / "run_1node_fragment_debug.pbs",
        root / "scripts" / "miyabi" / "run_2node_fragment_debug.pbs",
        root / "scripts" / "miyabi" / "run_9node_fragment_gpt2_wikitext2_50x4.pbs",
        root / "scripts" / "miyabi" / "run_9node_fragment_gpt2_wikitext2_5000steps.pbs",
    ]:
        assert path.exists()
