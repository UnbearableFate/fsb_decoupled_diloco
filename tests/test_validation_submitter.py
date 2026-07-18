import os
import subprocess


def test_submitter_creates_afterok_validation_dependency(tmp_path):
    train_script = tmp_path / "train.pbs"
    eval_script = tmp_path / "eval.pbs"
    fake_qsub = tmp_path / "qsub"
    qsub_log = tmp_path / "qsub.log"
    train_script.write_text("#!/bin/bash\n", encoding="utf-8")
    eval_script.write_text("#!/bin/bash\n", encoding="utf-8")
    fake_qsub.write_text(
        """#!/bin/bash
set -euo pipefail
printf '%s\n' "$*" >> "$QSUB_LOG"
if [[ "$*" == *"depend=afterok:"* ]]; then
  echo 222.eval
else
  echo 111.train
fi
""",
        encoding="utf-8",
    )
    fake_qsub.chmod(0o755)
    env = {
        **os.environ,
        "PROJECT_ROOT": str(tmp_path),
        "PYTHON_BIN": "/python",
        "RUN_ID": "paired-run",
        "SHARED_ROOT": str(tmp_path / "run"),
        "EVAL_SCRIPT": str(eval_script),
        "QSUB_BIN": str(fake_qsub),
        "QSUB_LOG": str(qsub_log),
        "TRAINING_SEED": "2027",
        "SYNCER_PUBLISH_DTYPE": "bfloat16",
        "STALENESS_LAMBDA": "4.0",
        "MAX_STALENESS_VERSIONS": "0",
        "GLOBAL_ADOPTION_STRATEGY": "predict_post_publish_global",
        "CAPTURE_TERMINAL_PREDECESSOR_FOR_EVAL": "true",
        "COMPLETION_MODE": "local_or_global",
    }

    completed = subprocess.run(
        ["bash", "scripts/miyabi/submit_train_with_validation.sh", str(train_script)],
        cwd=os.getcwd(),
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )

    calls = qsub_log.read_text(encoding="utf-8").splitlines()
    assert len(calls) == 2
    assert "RUN_ID=paired-run" in calls[0]
    assert "TRAINING_SEED=2027" in calls[0]
    assert "SYNCER_PUBLISH_DTYPE=bfloat16" in calls[0]
    assert "STALENESS_LAMBDA=4.0" in calls[0]
    assert "MAX_STALENESS_VERSIONS=0" in calls[0]
    assert "GLOBAL_ADOPTION_STRATEGY=predict_post_publish_global" in calls[0]
    assert "CAPTURE_TERMINAL_PREDECESSOR_FOR_EVAL=true" in calls[0]
    assert "COMPLETION_MODE=local_or_global" in calls[0]
    assert "-W depend=afterok:111.train" in calls[1]
    assert f"RUN_ROOT={tmp_path / 'run'}" in calls[1]
    assert "TRAIN_JOB_ID=111.train" in completed.stdout
    assert "VALIDATION_JOB_ID=222.eval" in completed.stdout
