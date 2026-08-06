"""Filesystem recovery claim arbitration with PBS reconciliation and budgets."""

from __future__ import annotations

import hashlib
import json
import os
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from ..core.config import RecoverySubmissionSection
from ..storage.atomic_io import atomic_write_json, safe_read_json
from ..storage.paths import RunPaths
from .pbs_scheduler import OUTSTANDING_CLASSIFICATIONS, PBSScheduler


def recovery_observation_key(
    *,
    run_id: str,
    highest_epoch: int,
    heartbeat_seq: int,
    heartbeat_fingerprint: str,
) -> str:
    payload = json.dumps(
        [run_id, int(highest_epoch), int(heartbeat_seq), heartbeat_fingerprint],
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


@dataclass(frozen=True)
class RecoveryClaimResult:
    state: str
    observation_key: str
    attempt: int | None = None
    attempt_dir: Path | None = None
    job_id: str | None = None


class RecoveryClaimManager:
    def __init__(
        self,
        *,
        paths: RunPaths,
        config: RecoverySubmissionSection,
        scheduler: PBSScheduler,
        descriptor_sha256: str,
        wall_clock: Callable[[], float] = time.time,
    ) -> None:
        self.paths = paths
        self.config = config
        self.scheduler = scheduler
        self.descriptor_sha256 = descriptor_sha256
        self._wall_clock = wall_clock

    @contextmanager
    def _global_reservation(self, *, claimant_id: str) -> Iterator[bool]:
        """Serialize the global outstanding-budget decision across observations."""

        root = self.paths.syncer_launch_claims
        if not root.is_dir():
            raise RuntimeError("recovery claim authority directory is missing")
        lock_path = root / ".global_reservation.lock"
        owner_id = f"{claimant_id}:{os.getpid()}:{uuid.uuid4()}"
        acquired = False
        try:
            try:
                lock_path.mkdir(exist_ok=False)
                acquired = True
            except FileExistsError:
                scheduler_timeout = float(getattr(self.scheduler, "timeout_seconds", 0.0))
                stale_after = max(
                    float(self.config.claim_timeout_seconds),
                    float(self.config.uncertainty_timeout_seconds),
                    2.0 * scheduler_timeout + 5.0,
                )
                try:
                    age = float(self._wall_clock()) - float(lock_path.stat().st_mtime)
                except FileNotFoundError:
                    age = -1.0
                if age >= stale_after:
                    stale_path = root / f".global_reservation.stale.{uuid.uuid4().hex}"
                    try:
                        lock_path.rename(stale_path)
                    except (FileNotFoundError, OSError):
                        pass
                    else:
                        for child in stale_path.iterdir():
                            if child.is_file():
                                child.unlink(missing_ok=True)
                        try:
                            stale_path.rmdir()
                        except OSError:
                            pass
                    try:
                        lock_path.mkdir(exist_ok=False)
                        acquired = True
                    except FileExistsError:
                        pass
            if not acquired:
                yield False
                return
            atomic_write_json(
                lock_path / "reservation.json",
                {
                    "format_version": 1,
                    "owner_id": owner_id,
                    "claimant_id": claimant_id,
                    "pid": os.getpid(),
                    "acquired_at": float(self._wall_clock()),
                },
            )
            yield True
        finally:
            if acquired:
                reservation_path = lock_path / "reservation.json"
                reservation = safe_read_json(reservation_path) or {}
                if reservation.get("owner_id") == owner_id:
                    reservation_path.unlink(missing_ok=True)
                    try:
                        lock_path.rmdir()
                    except OSError:
                        pass

    def maybe_submit(
        self,
        *,
        observation_key: str,
        claimant_id: str,
        terminal_published: bool,
    ) -> RecoveryClaimResult:
        if not self.config.enabled:
            return RecoveryClaimResult("disabled", observation_key)
        if terminal_published:
            return RecoveryClaimResult("terminal", observation_key)
        with self._global_reservation(claimant_id=claimant_id) as reserved:
            if not reserved:
                return RecoveryClaimResult("reservation_busy", observation_key)
            # The current observation can remain unchanged for arbitrarily long
            # after a heartbeat stalls.  Keep its attempt directories until a
            # strictly newer observation is seen so archival cannot reset the
            # per-observation submission budget.
            self._archive_expired_claims(
                preserve_observation_key=observation_key,
            )
            root = self.paths.syncer_launch_claims / observation_key
            attempts = self._attempts(root)
            now = float(self._wall_clock())
            outstanding = self._outstanding_attempts(self._all_attempts(), now=now)
            if len(outstanding) >= self.config.max_outstanding_candidates:
                latest = outstanding[-1]
                receipt = safe_read_json(latest / "submission.json") or {}
                return RecoveryClaimResult(
                    "outstanding",
                    observation_key,
                    attempt=self._attempt_number(latest),
                    attempt_dir=latest,
                    job_id=receipt.get("job_id_raw"),
                )
            if attempts:
                latest = attempts[-1]
                claim = safe_read_json(latest / "claim.json") or {}
                claimed_at = self._claimed_at(latest, claim, now=now)
                if now - claimed_at < self.config.claim_timeout_seconds:
                    return RecoveryClaimResult(
                        "claim_live",
                        observation_key,
                        attempt=self._attempt_number(latest),
                        attempt_dir=latest,
                    )
                delay = min(
                    self.config.backoff_max_seconds,
                    self.config.backoff_initial_seconds
                    * (2 ** max(0, self._attempt_number(latest) - 1)),
                )
                if now - claimed_at < delay:
                    return RecoveryClaimResult("backoff", observation_key)
            attempt = len(attempts) + 1
            if attempt > self.config.max_attempts_per_observation:
                return RecoveryClaimResult("budget_exhausted", observation_key)
            attempt_dir = root / f"attempt_{attempt:06d}.lock"
            try:
                attempt_dir.mkdir(parents=True, exist_ok=False)
            except FileExistsError:
                return RecoveryClaimResult(
                    "lost_race", observation_key, attempt=attempt, attempt_dir=attempt_dir
                )
            claim = {
                "format_version": 1,
                "observation_key": observation_key,
                "attempt": attempt,
                "claimant_id": claimant_id,
                "pid": os.getpid(),
                "claimed_at": now,
                "request_fingerprint": f"{observation_key}:{attempt}",
            }
            atomic_write_json(attempt_dir / "claim.json", claim)
        submission = self.scheduler.submit_candidate(
            script=self.config.candidate_pbs_script,
            request_fingerprint=claim["request_fingerprint"],
            shared_root=self.paths.shared_root,
            descriptor_sha256=self.descriptor_sha256,
            walltime=str(self.config.candidate_walltime),
        )
        receipt = {**submission, "submitted_at": float(self._wall_clock())}
        atomic_write_json(attempt_dir / "submission.json", receipt)
        if submission.get("returncode") != 0 or not submission.get("job_id_raw"):
            return RecoveryClaimResult(
                "submission_failed",
                observation_key,
                attempt=attempt,
                attempt_dir=attempt_dir,
            )
        return RecoveryClaimResult(
            "submitted",
            observation_key,
            attempt=attempt,
            attempt_dir=attempt_dir,
            job_id=str(submission["job_id_raw"]),
        )

    def _outstanding_attempts(self, attempts: list[Path], *, now: float) -> list[Path]:
        outstanding: list[Path] = []
        for attempt in attempts:
            claim = safe_read_json(attempt / "claim.json") or {}
            receipt = safe_read_json(attempt / "submission.json")
            if receipt is None:
                claimed_at = self._claimed_at(attempt, claim, now=now)
                age = now - claimed_at
                if age < self.config.uncertainty_timeout_seconds:
                    outstanding.append(attempt)
                    continue
                find = getattr(self.scheduler, "find_by_request_fingerprint", None)
                request_fingerprint = str(claim.get("request_fingerprint", ""))
                observation = (
                    None if find is None or not request_fingerprint else find(request_fingerprint)
                )
                if observation is not None and observation.classification in (
                    OUTSTANDING_CLASSIFICATIONS
                ):
                    atomic_write_json(
                        attempt / "submission.json",
                        {
                            "returncode": 0,
                            "job_id_raw": observation.job_id,
                            "job_id_normalized": observation.job_id,
                            "request_fingerprint": request_fingerprint,
                            "submitted_at": claimed_at,
                            "reconciled": True,
                        },
                    )
                    outstanding.append(attempt)
                continue
            job_id = receipt.get("job_id_raw")
            if not job_id:
                continue
            observation = self._query_job(str(job_id))
            if observation.classification in OUTSTANDING_CLASSIFICATIONS:
                outstanding.append(attempt)
            elif observation.classification in {"query_failed", "unknown"}:
                submitted_at = float(receipt.get("submitted_at", 0.0))
                if now - submitted_at < self.config.uncertainty_timeout_seconds:
                    outstanding.append(attempt)
        return outstanding

    def _query_job(self, job_id: str):
        observation = self.scheduler.query(job_id)
        if observation.classification not in {"query_failed", "no_record", "unknown"}:
            return observation
        try:
            historical = self.scheduler.query(job_id, historical=True)
        except TypeError:
            return observation
        if historical.classification in OUTSTANDING_CLASSIFICATIONS | {"finished"}:
            return historical
        return observation

    @staticmethod
    def _attempt_number(path: Path) -> int:
        return int(path.name.removeprefix("attempt_").removesuffix(".lock"))

    @staticmethod
    def _claimed_at(
        attempt: Path,
        claim: dict[str, Any],
        *,
        now: float,
    ) -> float:
        try:
            recorded = float(claim.get("claimed_at", 0.0))
        except (TypeError, ValueError):
            recorded = 0.0
        if recorded > 0.0:
            return recorded
        # mkdir is the arbitration point and precedes claim.json.  During that
        # small publication window (or after a claimant crash) use the atomic
        # directory's timestamp instead of treating a missing claim as epoch 0
        # and incorrectly opening attempt N+1.
        try:
            return min(float(attempt.stat().st_mtime), float(now))
        except FileNotFoundError:
            return float(now)

    def _attempts(self, root: Path) -> list[Path]:
        if not root.is_dir():
            return []
        return sorted(
            (path for path in root.glob("attempt_*.lock") if path.is_dir()),
            key=self._attempt_number,
        )

    def _all_attempts(self) -> list[Path]:
        root = self.paths.syncer_launch_claims
        if not root.is_dir():
            return []
        return sorted(
            (path for path in root.glob("*/attempt_*.lock") if path.is_dir()),
            key=lambda path: (path.parent.name, self._attempt_number(path)),
        )

    def _archive_expired_claims(
        self,
        *,
        preserve_observation_key: str | None = None,
    ) -> int:
        root = self.paths.syncer_launch_claims
        if not root.is_dir():
            return 0
        now = float(self._wall_clock())
        archived: list[dict[str, Any]] = []
        directories: list[Path] = []
        for attempt in sorted(root.glob("*/attempt_*.lock")):
            if not attempt.is_dir():
                continue
            if attempt.parent.name == preserve_observation_key:
                continue
            claim = safe_read_json(attempt / "claim.json") or {}
            claimed_at = self._claimed_at(attempt, claim, now=now)
            if now - claimed_at < self.config.claim_retention_seconds:
                continue
            # This also reconciles the qsub-success/receipt-missing window by
            # request fingerprint.  Never archive a claim while its physical
            # job is still queued, in prologue, running, suspended, or within
            # the configured scheduler-uncertainty window.
            if self._outstanding_attempts([attempt], now=now):
                continue
            receipt = safe_read_json(attempt / "submission.json")
            archived.append(
                {
                    "record_kind": "recovery_submission",
                    "archived_at": now,
                    "claim": claim,
                    "submission": receipt,
                }
            )
            directories.append(attempt)
        if not archived:
            return 0
        history = self.paths.recovery_submission_history_jsonl
        history.parent.mkdir(parents=True, exist_ok=True)
        with history.open("a", encoding="utf-8") as handle:
            for row in archived:
                handle.write(json.dumps(row, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        for directory in directories:
            (directory / "submission.json").unlink(missing_ok=True)
            (directory / "claim.json").unlink(missing_ok=True)
            try:
                directory.rmdir()
            except OSError:
                continue
            try:
                directory.parent.rmdir()
            except OSError:
                pass
        return len(directories)
