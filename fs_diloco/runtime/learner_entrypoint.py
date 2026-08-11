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
    publish_admission_request_with_sha256,
    read_admission_response,
)
from ..storage.paths import prepare_learner_instance_dir


def build_parser() -> argparse.ArgumentParser:
    """Build the sole stream-admission learner command line."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--shared-root", required=True)
    parser.add_argument("--bootstrap-slot", type=int)
    parser.add_argument("--launch-request-id")
    parser.add_argument("--stream-id", type=int)
    parser.add_argument("--replace-instance-id")
    return parser


def main(argv: list[str] | None = None) -> None:
    """Complete and revalidate admission before importing the training runtime."""

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
    if (args.bootstrap_slot is None) == (args.launch_request_id is None):
        raise ValueError("learner requires exactly one of --bootstrap-slot or --launch-request-id")
    if args.bootstrap_slot is not None:
        if args.stream_id is not None or args.replace_instance_id is not None:
            raise ValueError(
                "bootstrap admission derives its stream and cannot replace an instance"
            )
        stream_id = args.bootstrap_slot
    else:
        if args.stream_id is None:
            raise ValueError("launch-authorized admission requires --stream-id")
        stream_id = args.stream_id
    if not 0 <= stream_id < int(descriptor["stream_pool_size"]):
        raise ValueError("stream is outside the immutable descriptor pool")
    actor_id = f"learner_li_{uuid.uuid4()}"
    token_sha = hashlib.sha256(os.urandom(32)).hexdigest()
    prepare_learner_instance_dir(loaded.paths, str(stream_id))
    _request_path, request_sha256 = publish_admission_request_with_sha256(
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
    timeout = (
        shared.membership.initial_membership_deadline_seconds
        if args.bootstrap_slot is not None
        else shared.membership.registration_request_ttl_seconds
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
