"""Torch-free learner admission entrypoint for the Full Protocol."""

from __future__ import annotations

import argparse
import hashlib
import os
import sys
import time
import uuid
from pathlib import Path

from ..core.run_descriptor import load_run_descriptor
from ..storage.admission import (
    highest_static_generation,
    new_attempt_id,
    publish_dynamic_request_with_sha256,
    publish_static_request_with_sha256,
    read_admission_response,
    static_logical_launch_id,
)
from ..storage.paths import prepare_learner_instance_dir


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--shared-root", required=True)
    parser.add_argument("--learner-id")
    parser.add_argument("--logical-launch-id")
    parser.add_argument("--attempt-id")
    parser.add_argument("--bootstrap-slot", type=int)
    parser.add_argument("--launch-request-id")
    parser.add_argument("--stream-id", type=int)
    parser.add_argument("--replace-instance-id")
    return parser


def main(argv: list[str] | None = None) -> None:
    if "torch" in sys.modules:
        raise RuntimeError("learner entrypoint imported torch before descriptor/admission gate")
    args = build_parser().parse_args(argv)
    loaded = load_run_descriptor(
        args.shared_root,
        expected_descriptor_sha256=os.environ.get("FS_DILOCO_EXPECTED_DESCRIPTOR_SHA256"),
        expected_git_commit=os.environ.get("FS_DILOCO_EXPECTED_GIT_COMMIT"),
        expected_source_fingerprint=os.environ.get("FS_DILOCO_EXPECTED_SOURCE_FINGERPRINT"),
    )
    config_path = Path(args.config).resolve()
    if config_path != loaded.paths.resolved_config_yaml.resolve():
        raise RuntimeError("learner must use the immutable resolved descriptor config")
    descriptor = loaded.descriptor
    config = loaded.config
    shared = config
    dynamic = shared.membership.mode == "dynamic"
    if dynamic != (descriptor["mode"] == "dynamic"):
        raise RuntimeError("descriptor membership mode mismatch")
    attempt_id = args.attempt_id or new_attempt_id()
    if dynamic:
        if args.learner_id is not None or args.logical_launch_id is not None:
            raise ValueError("dynamic learner rejects static identity arguments")
        if (args.bootstrap_slot is None) == (args.launch_request_id is None):
            raise ValueError(
                "dynamic learner requires exactly one of --bootstrap-slot or --launch-request-id"
            )
        stream_id = args.bootstrap_slot if args.bootstrap_slot is not None else args.stream_id
        if stream_id is None:
            raise ValueError("replacement launch requires --stream-id")
        if not 0 <= stream_id < int(descriptor["stream_pool_size"]):
            raise ValueError("dynamic stream is outside the immutable descriptor pool")
        actor_id = f"learner_li_{uuid.uuid4()}"
        token_sha = hashlib.sha256(os.urandom(32)).hexdigest()
        prepare_learner_instance_dir(loaded.paths, str(stream_id))
        _request_path, request_sha256 = publish_dynamic_request_with_sha256(
            loaded.paths,
            run_id=str(descriptor["run_id"]),
            descriptor_sha256=str(descriptor["descriptor_sha256"]),
            instance_id=actor_id,
            stream_id=stream_id,
            admission_token_sha256=token_sha,
            bootstrap_slot=args.bootstrap_slot,
            launch_request_id=args.launch_request_id,
            replace_instance_id=args.replace_instance_id,
        )
        response_attempt_id = actor_id
        response_stable_key = str(stream_id)
    else:
        if args.learner_id is None:
            raise ValueError("static learner requires --learner-id")
        if args.bootstrap_slot is not None or args.launch_request_id is not None:
            raise ValueError("static learner rejects dynamic admission arguments")
        actor_id = args.learner_id
        if actor_id not in descriptor["static_learner_ids"]:
            raise ValueError("static learner is outside the immutable descriptor scope")
        prepare_learner_instance_dir(loaded.paths, actor_id)
        logical_launch_id = args.logical_launch_id or static_logical_launch_id(
            descriptor_sha256=str(descriptor["descriptor_sha256"]),
            learner_id=actor_id,
        )
        observed_generation = highest_static_generation(loaded.paths, actor_id)
        _request_path, request_sha256 = publish_static_request_with_sha256(
            loaded.paths,
            run_id=str(descriptor["run_id"]),
            descriptor_sha256=str(descriptor["descriptor_sha256"]),
            learner_id=actor_id,
            logical_launch_id=logical_launch_id,
            attempt_id=attempt_id,
            expected_generation=observed_generation,
        )
        response_attempt_id = attempt_id
        response_stable_key = actor_id
    timeout = (
        shared.membership.initial_membership_deadline_seconds
        if dynamic and args.bootstrap_slot is not None
        else (
            shared.membership.registration_request_ttl_seconds
            if dynamic
            else config.leader.learner_recovery_wait_seconds
        )
    )
    deadline = time.monotonic() + float(timeout)
    admission = None
    while time.monotonic() < deadline:
        admission = read_admission_response(
            loaded.paths,
            run_id=str(descriptor["run_id"]),
            descriptor_sha256=str(descriptor["descriptor_sha256"]),
            actor_id=actor_id,
            attempt_id=response_attempt_id,
            stable_contributor_key=response_stable_key,
            request_sha256=request_sha256,
            max_clock_skew_seconds=config.leader.max_clock_skew_seconds,
        )
        if admission is not None:
            break
        time.sleep(
            min(
                float(shared.membership.registration_scan_interval_seconds),
                float(shared.sync.scan_interval_seconds),
            )
        )
    if admission is None:
        raise TimeoutError(f"learner admission timed out before torch import: {actor_id}")
    if "torch" in sys.modules:
        raise RuntimeError("learner admission gate imported torch")
    current_admission = read_admission_response(
        loaded.paths,
        run_id=str(descriptor["run_id"]),
        descriptor_sha256=str(descriptor["descriptor_sha256"]),
        actor_id=actor_id,
        attempt_id=response_attempt_id,
        stable_contributor_key=response_stable_key,
        request_sha256=request_sha256,
        max_clock_skew_seconds=config.leader.max_clock_skew_seconds,
        expected_fence=admission.fence,
    )
    if current_admission != admission:
        raise RuntimeError("learner admission changed immediately before torch import")
    if "torch" in sys.modules:
        raise RuntimeError("learner admission revalidation imported torch")
    from .learner import run_admitted_learner

    run_admitted_learner(loaded, admission)


if __name__ == "__main__":
    main()
