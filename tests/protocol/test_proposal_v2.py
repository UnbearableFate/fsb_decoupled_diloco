from __future__ import annotations

import math

import pytest

from fs_diloco.protocol.proposal import FullUpdateProposalV2
from tests.support.v4_protocol import proposal_payload


def test_proposal_v2_strict_round_trip() -> None:
    proposal = FullUpdateProposalV2.from_dict(proposal_payload())

    assert FullUpdateProposalV2.from_json(proposal.canonical_bytes()) == proposal
    assert len(proposal.immutable_sha256()) == 64


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("effective_tokens_this_update", 0, "must be >= 1"),
        ("processed_tokens_this_cycle", -1, "must be >= 1"),
        ("inner_steps", 0, "must be >= 1"),
        ("created_at", math.nan, "must be finite"),
        ("created_at", math.inf, "must be finite"),
        ("base_global_version", True, "must be an integer"),
    ],
)
def test_proposal_v2_rejects_invalid_scalars(field: str, value: object, message: str) -> None:
    payload = proposal_payload()
    payload[field] = value

    with pytest.raises(ValueError, match=message):
        FullUpdateProposalV2.from_dict(payload)


def test_proposal_v2_rejects_step_cursor_and_token_mismatch() -> None:
    step = proposal_payload()
    step["local_step_end"] = 9
    with pytest.raises(ValueError, match="local_step_end"):
        FullUpdateProposalV2.from_dict(step)

    cursor = proposal_payload()
    cursor["data_cursor_end"] = cursor["data_cursor_start"]
    with pytest.raises(ValueError, match="data_cursor_end"):
        FullUpdateProposalV2.from_dict(cursor)

    tokens = proposal_payload()
    tokens["processed_tokens_this_cycle"] = 9
    with pytest.raises(ValueError, match="processed tokens"):
        FullUpdateProposalV2.from_dict(tokens)


@pytest.mark.parametrize(
    "path",
    [
        "/tmp/update.safetensors",
        "../update.safetensors",
        "updates/staging/update.safetensors",
        "updates/payloads/learner-0/not-the-update.safetensors",
    ],
)
def test_proposal_v2_rejects_untrusted_payload_paths(path: str) -> None:
    payload = proposal_payload()
    payload["payload_relative_path"] = path

    with pytest.raises(ValueError, match="payload_relative_path|staging"):
        FullUpdateProposalV2.from_dict(payload)


def test_proposal_v2_rejects_unknown_fields_versions_and_duplicate_json_keys() -> None:
    unknown = proposal_payload()
    unknown["future"] = 1
    with pytest.raises(ValueError, match="unknown fields"):
        FullUpdateProposalV2.from_dict(unknown)

    version = proposal_payload()
    version["proposal_format_version"] = 3
    with pytest.raises(ValueError, match="unsupported proposal_format_version"):
        FullUpdateProposalV2.from_dict(version)

    with pytest.raises(ValueError, match="duplicate key"):
        FullUpdateProposalV2.from_json('{"proposal_format_version":2,"proposal_format_version":2}')


def test_proposal_v2_enforces_json_size_limit() -> None:
    with pytest.raises(ValueError, match="exceeds"):
        FullUpdateProposalV2.from_json("{} " * 100, max_bytes=8)
