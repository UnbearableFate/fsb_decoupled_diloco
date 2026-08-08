from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from fs_diloco.protocol.selection import PersistentFairSelector
from fs_diloco.runtime.launch_outbox import LearnerLaunchOutbox
from fs_diloco.runtime.pbs_scheduler import PBSJobObservation
from tests.support import DynamicAuthorityHarness, FakePBS
from tests.storage.test_authority_p2_dynamic import (
    test_revoke_after_selection_returns_per_row_conflict_then_retry_commits as _p2_revoke_after,
    test_revoke_before_selection_leaves_current_quorum_progressing as _p2_revoke_before,
)
from tests.storage.test_visibility_v4 import (
    test_transient_recovery_never_drops_and_deadline_enters_manual_review as _p2_transient,
)


pytestmark = pytest.mark.plan03_red


def test_h01a_revoke_before_select_does_not_abort_current_batch(tmp_path: Path) -> None:
    _p2_revoke_before(tmp_path)


def test_h01b_commit_conflict_terminalizes_only_invalid_rows(tmp_path: Path) -> None:
    _p2_revoke_after(tmp_path)


def test_h05_quorum_truncation_serves_every_continuously_ready_contributor() -> None:
    contributors = tuple(f"learner_{index:03d}" for index in range(8))
    selector = PersistentFairSelector()
    selected_contributors: set[str] = set()
    for version in range(1, 1001):
        selected = selector.select(contributors, quorum_max=3)
        selected_contributors.update(selected)
        selector.commit(selected, committed_version=version)
    assert selected_contributors == set(contributors)


def test_h06_transient_registration_eio_preserves_request_for_retry(
    tmp_path: Path,
) -> None:
    _p2_transient(tmp_path)


def test_h07_known_job_no_record_enters_bounded_uncertainty(tmp_path: Path) -> None:
    authority = DynamicAuthorityHarness.create(tmp_path / "authority-h07")
    try:
        launch = authority.store.initialize_dynamic_membership(
            stream_pool_size=1,
            bootstrap_instances=1,
            config_fingerprint="descriptor-digest",
            created_at=100.0,
        )[0]
        authority.store.record_external_launch_jobs(
            [
                {
                    "bootstrap_slot": 0,
                    "request_id": str(launch["request_id"]),
                    "pbs_job_id": "known.opbs",
                }
            ],
            observed_at=100.0,
        )
        scheduler = FakePBS()
        no_record = PBSJobObservation("known.opbs", "no_record", None, 1, "missing")
        scheduler.queue_query("known.opbs", no_record)
        scheduler.queue_query("known.opbs", no_record, historical=True)
        outbox = LearnerLaunchOutbox(
            paths=authority.paths,
            config=SimpleNamespace(
                scheduler_reconcile_interval_seconds=1.0,
                learner_pbs_script="learner.pbs",
                learner_walltime="00:01:00",
            ),
            scheduler=scheduler,
            descriptor_sha256="descriptor-digest",
            wall_clock=lambda: 101.0,
        )
        outbox.reconcile(authority.store)
        row = next(
            item
            for item in authority.store.launch_requests()
            if item["request_id"] == launch["request_id"]
        )
        assert row["state"] == "terminal_uncertain"
        assert row["reservation_released_at"] is None
        assert row["uncertainty_deadline"] is not None
    finally:
        authority.close()
