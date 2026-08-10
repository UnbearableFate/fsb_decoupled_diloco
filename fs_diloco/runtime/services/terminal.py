"""Full Protocol terminal close, drain, and bounded final merge service."""

from __future__ import annotations

import hashlib
import json
import time
from typing import Any, Callable

from ...core.run_descriptor import LoadedRunDescriptor
from ...core.versions import CONTROL_FORMAT_VERSION
from ...protocol.contributor import decode_contributor_fence
from ...storage.authority import LeaderAuthority, LeaderSession
from ...storage.control import ControlPublisher, iter_terminal_acks
from ...storage.terminal_request import read_manual_terminal_request
from .merge import MergeAttemptStatus, MergeService


def terminal_close_reason(
    loaded: LoadedRunDescriptor,
    authority: LeaderAuthority,
    *,
    version: int,
    now: float | None = None,
) -> str | None:
    """Evaluate the configured close policy without mutating authority state."""

    config = loaded.config
    controller = authority.read.controller_status()
    if controller["state"] in {"preclosing", "closing", "draining"}:
        return str(controller.get("reason") or "resumed_terminal_close")
    policy = config.terminal.admission_close_policy
    target = config.sync.stop_after_outer_steps
    token_target = config.sync.stop_after_direct_weight_tokens_applied
    target_reached = (target is not None and version >= target) or (
        token_target is not None
        and authority.read.token_ledger_summary().direct_applied >= token_target
    )
    if policy in {"global_target", "global_target_or_launch_budget"} and target_reached:
        return "configured_target"
    if policy == "manual":
        request = read_manual_terminal_request(
            loaded.paths,
            run_id=str(loaded.descriptor["run_id"]),
            descriptor_sha256=str(loaded.descriptor["descriptor_sha256"]),
        )
        if request is not None:
            return f"manual:{request['request_id']}:{request['reason']}"
    if policy == "deadline":
        started = authority.read.genesis_committed_at()
        if started is not None and float(now if now is not None else time.time()) >= (
            started + float(config.terminal.deadline_seconds)
        ):
            return "configured_deadline"
    if policy == "global_target_or_launch_budget" and config.scaling.enabled:
        launches = [
            row for row in authority.read.dynamic_launch_requests() if row["role"] != "bootstrap"
        ]
        active = [row for row in launches if row["reservation_released_at"] is None]
        observations = authority.read.capacity_observations()
        latest_capacity = None if not observations else observations[-1]
        capacity_still_low = latest_capacity is not None and (
            int(latest_capacity["productive_instances"])
            + int(latest_capacity["reserved_launch_capacity"])
            < int(latest_capacity["desired_contributors"])
        )
        if (
            len(launches) >= config.scaling.max_total_launch_requests
            and not active
            and capacity_still_low
        ):
            return "launch_budget_exhausted"
    return None


class TerminalService:
    def __init__(
        self,
        *,
        loaded: LoadedRunDescriptor,
        authority: LeaderAuthority,
        leader: LeaderSession,
        control: ControlPublisher,
        telemetry: Any,
        merge: MergeService,
        ingest: Callable[[], None],
        admit_preclose: Callable[[float], None],
        monotonic_clock: Any = time.monotonic,
        wall_clock: Any = time.time,
        sleep: Any = time.sleep,
    ) -> None:
        self.loaded = loaded
        self.authority = authority
        self.leader = leader
        self.control = control
        self.telemetry = telemetry
        self.merge = merge
        self.ingest = ingest
        self.admit_preclose = admit_preclose
        self.monotonic_clock = monotonic_clock
        self.wall_clock = wall_clock
        self.sleep = sleep

    def finalize(self, *, reason: str) -> dict[str, Any]:
        terminal_config = self.loaded.config.terminal
        controller = self.authority.read.controller_status()
        cycle_token_budget = (
            int(self.loaded.config.training.inner_steps)
            * int(self.loaded.config.training.gradient_accumulation_steps)
            * int(self.loaded.config.training.micro_batch_size)
            * int(self.loaded.config.data.block_size)
        )
        close_command = self._command_id("terminal-close", reason)
        preclose_command = self._command_id("terminal-preclose", reason)
        if controller["state"] == "open" and (
            terminal_config.allow_preclose_admission_during_drain
        ):
            self.leader.begin_terminal_preclose(
                command_id=preclose_command,
                reason=reason,
                registration_visibility_grace_seconds=float(
                    terminal_config.registration_visibility_grace_seconds
                ),
                hard_crash_cycle_token_budget=cycle_token_budget,
            )
            controller = self.authority.read.controller_status()
        if controller["state"] == "preclosing":
            close_intent_at = float(controller["requested_at"])
            visibility_deadline = float(controller["registration_visibility_deadline"])
            while True:
                self.admit_preclose(close_intent_at)
                remaining = visibility_deadline - float(self.wall_clock())
                if remaining <= 0.0:
                    break
                self.sleep(
                    min(
                        remaining,
                        float(self.loaded.config.sync.scan_interval_seconds),
                    )
                )
            controller = self.authority.read.controller_status()
        if controller["state"] in {"open", "preclosing"}:
            self.leader.begin_terminal_close(
                command_id=close_command,
                reason=reason,
                hard_crash_cycle_token_budget=cycle_token_budget,
                drain_ack_timeout_seconds=float(terminal_config.drain_ack_timeout_seconds),
            )
            controller = self.authority.read.controller_status()
        self.control.publish_drain(controller)
        ack_deadline = float(controller["drain_ack_deadline"])
        while float(self.wall_clock()) < ack_deadline:
            self.ingest()
            self._ingest_acks(controller)
            if not any(
                row["state"] == "awaiting_ack"
                for row in self.authority.read.terminal_contributor_fences()
            ):
                break
            remaining = ack_deadline - float(self.wall_clock())
            if remaining <= 0.0:
                break
            self.sleep(min(remaining, float(self.loaded.config.sync.scan_interval_seconds)))
        self._adjudicate_hard_crashes(cycle_token_budget)

        controller = self.authority.read.controller_status()
        generation = int(controller["generation"])
        proposal_deadline = self.leader.begin_terminal_proposal_visibility(
            command_id=f"terminal-proposal-visibility-g{generation}",
            generation=generation,
            proposal_visibility_grace_seconds=float(
                terminal_config.proposal_visibility_grace_seconds
            ),
        )
        while float(self.wall_clock()) < proposal_deadline:
            self.ingest()
            if self._all_declared_final_updates_visible():
                break
            remaining = proposal_deadline - float(self.wall_clock())
            if remaining <= 0.0:
                break
            self.sleep(min(remaining, float(self.loaded.config.sync.scan_interval_seconds)))
        maximum = int(terminal_config.max_terminal_merges)
        controller = self.authority.read.controller_status()
        remaining_merges = maximum - int(controller["terminal_merge_count"])
        conflict_budget = max(1, len(self.authority.read.current_contributor_fences()) + 1)
        while remaining_merges > 0:
            self.ingest()
            outcome = self.merge.merge_once(
                quorum_min=1,
                quorum_max=self.loaded.config.sync.quorum_max,
                purpose="terminal",
            )
            if outcome is MergeAttemptStatus.NO_BATCH:
                break
            if outcome is MergeAttemptStatus.FENCE_CONFLICT:
                if conflict_budget <= 0:
                    raise RuntimeError("terminal merge conflicts exceeded the bounded retry budget")
                conflict_budget -= 1
                continue
            controller = self.authority.read.controller_status()
            remaining_merges = maximum - int(controller["terminal_merge_count"])
        state = self.authority.read.controller_status()["state"]
        if state not in {"finalized", "error"}:
            self.leader.finalize_terminal(
                command_id=self._command_id("terminal-finalize", reason),
                reason=reason,
            )
        terminal = self.authority.read.terminal_record()
        if terminal is None:
            raise RuntimeError("terminal record was not committed")
        return terminal

    @staticmethod
    def _command_id(prefix: str, reason: str) -> str:
        return f"{prefix}-{hashlib.sha256(reason.encode('utf-8')).hexdigest()}"

    def _ingest_acks(self, controller: dict[str, Any]) -> None:
        for path, payload, fence in iter_terminal_acks(self.loaded.paths):
            try:
                generation = payload.get("generation")
                final_cycle_seq = payload.get("final_cycle_seq")
                if (
                    payload.get("format_version") != CONTROL_FORMAT_VERSION
                    or payload.get("kind") != "terminal_ack"
                    or payload.get("run_id") != self.loaded.descriptor["run_id"]
                    or payload.get("descriptor_sha256")
                    != self.loaded.descriptor["descriptor_sha256"]
                    or isinstance(generation, bool)
                    or not isinstance(generation, int)
                    or generation != int(controller["generation"])
                    or isinstance(final_cycle_seq, bool)
                    or not isinstance(final_cycle_seq, int)
                    or final_cycle_seq < 0
                    or not isinstance(payload.get("actor_id"), str)
                    or not isinstance(payload.get("attempt_id"), str)
                    or not (
                        payload.get("final_update_id") is None
                        or isinstance(payload.get("final_update_id"), str)
                    )
                ):
                    continue
                actor_id = payload["actor_id"]
                attempt_id = payload["attempt_id"]
                if fence.kind == "static":
                    if actor_id != fence.learner_id or attempt_id != fence.attempt_id:
                        continue
                elif actor_id != fence.instance_id or attempt_id != fence.instance_id:
                    continue
                ack_digest = hashlib.sha256(path.read_bytes()).hexdigest()
                self.leader.acknowledge_terminal_contributor(
                    command_id=f"terminal-ack-{ack_digest}",
                    fence=fence,
                    final_cycle_seq=final_cycle_seq,
                    final_update_id=payload.get("final_update_id"),
                )
                self.telemetry.event(
                    "terminal_ack_ingested",
                    contributor_actor_id=actor_id,
                    final_cycle_seq=final_cycle_seq,
                )
            except Exception as exc:
                self.telemetry.event(
                    "terminal_ack_rejected",
                    path=str(path),
                    error_type=type(exc).__name__,
                    error=str(exc),
                )

    def _adjudicate_hard_crashes(self, cycle_token_budget: int) -> None:
        for row in self.authority.read.terminal_contributor_fences():
            if row["state"] != "awaiting_ack":
                continue
            fence = decode_contributor_fence(json.loads(str(row["fence_json"])))
            ack_digest = hashlib.sha256(
                json.dumps(fence.as_dict(), sort_keys=True).encode()
            ).hexdigest()
            self.leader.acknowledge_terminal_contributor(
                command_id=f"terminal-hard-crash-{ack_digest}",
                fence=fence,
                final_cycle_seq=None,
                hard_crash_gap_tokens_upper_bound=cycle_token_budget,
            )
            self.telemetry.event(
                "terminal_hard_crash_adjudicated",
                stable_contributor_key=fence.stable_contributor_key,
                hard_crash_gap_tokens_upper_bound=cycle_token_budget,
            )

    def _all_declared_final_updates_visible(self) -> bool:
        declared = [
            str(row["final_update_id"])
            for row in self.authority.read.terminal_contributor_fences()
            if row["state"] == "acked" and row["final_update_id"] is not None
        ]
        return all(
            self.authority.read.update_status(update_id) is not None for update_id in declared
        )
