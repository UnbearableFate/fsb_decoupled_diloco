from __future__ import annotations

from fs_diloco.storage.artifact_policy import (
    ArtifactClass,
    ArtifactPolicy,
    build_artifact_policy,
)


PLAN03_REQUIREMENTS = frozenset({"AUDIT-01"})


def test_artifact_policy_classifies_protocol_domains_and_fails_unknown_closed() -> None:
    policy = ArtifactPolicy.from_dict(build_artifact_policy())

    assert policy.classify("control/syncer_metadata.sqlite3") is ArtifactClass.AUTHORITY
    assert policy.classify("audit/batches/history/batch.json") is ArtifactClass.AUDIT
    assert policy.classify("metrics/learner/learner-0/attempt.jsonl") is ArtifactClass.TELEMETRY
    assert policy.classify("heartbeats/learner-0.json") is ArtifactClass.CACHE
    assert policy.classify("updates/payloads/learner-0/update.bin") is ArtifactClass.PAYLOAD
    assert policy.classify("weights/.tmp-checkpoint") is ArtifactClass.TEMPORARY
    assert policy.classify("foreign/object.bin") is ArtifactClass.UNKNOWN
    assert not policy.allows_generic_cleanup("control/syncer_metadata.sqlite3")
    assert not policy.allows_generic_cleanup("audit/batches/history/batch.json")
    assert not policy.allows_generic_cleanup("foreign/object.bin")
    assert policy.allows_generic_cleanup("metrics/learner/learner-0/attempt.jsonl")


def test_artifact_policy_checksum_tamper_fails_closed() -> None:
    payload = build_artifact_policy()
    payload["classes"]["audit"].append("foreign/**")

    try:
        ArtifactPolicy.from_dict(payload)
    except ValueError as exc:
        assert "checksum" in str(exc)
    else:
        raise AssertionError("tampered artifact policy was accepted")
