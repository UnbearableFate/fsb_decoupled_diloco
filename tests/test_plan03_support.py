from __future__ import annotations

import errno
import math

import pytest

from tests.support import DeterministicIds, FaultTape, VirtualClock, paired_noninferiority


def test_virtual_clock_separates_timeout_and_audit_clock() -> None:
    clock = VirtualClock(monotonic_seconds=10.0, wall_seconds=100.0)
    clock.jump_wall(-50.0)
    assert clock.monotonic() == 10.0
    assert clock.wall() == 50.0
    clock.advance(2.5)
    assert clock.monotonic() == 12.5
    assert clock.wall() == 52.5
    with pytest.raises(ValueError, match="backwards"):
        clock.advance(-1.0)


def test_fault_tape_and_deterministic_ids_are_replayable() -> None:
    tape = FaultTape([("read", OSError(errno.ESTALE, "stale")), ("read", b"ok")])
    with pytest.raises(OSError) as error:
        tape.at("read")
    assert error.value.errno == errno.ESTALE
    assert tape.at("read") == b"ok"
    assert tape.exhausted()
    ids = DeterministicIds("oracle")
    assert [ids.next("proposal") for _ in range(2)] == [
        "oracle-proposal-000000",
        "oracle-proposal-000001",
    ]


def test_paired_performance_keeps_signed_deltas_and_fixed_bootstrap() -> None:
    result = paired_noninferiority(
        [10.0, 10.0, 10.0, 10.0, 10.0],
        [9.0, 10.2, 10.5, 10.8, 11.0],
        bootstrap_samples=1_000,
    )
    assert result.signed_overheads[0] == pytest.approx(-0.1)
    assert result.median_overhead == pytest.approx(0.05)
    assert result.bootstrap_upper_95 >= result.median_overhead
    assert result.margin == 0.10


@pytest.mark.parametrize(
    ("baseline", "candidate"),
    [
        ([0.0], [1.0]),
        ([-1.0], [1.0]),
        ([math.nan], [1.0]),
        ([math.inf], [1.0]),
        ([1.0], [0.0]),
        ([1.0], [-1.0]),
        ([1.0], [math.nan]),
        ([1.0], [math.inf]),
    ],
)
def test_paired_performance_rejects_nonpositive_or_nonfinite_durations(
    baseline: list[float],
    candidate: list[float],
) -> None:
    with pytest.raises(ValueError, match="positive and finite"):
        paired_noninferiority(baseline, candidate)


def test_paired_performance_rejects_invalid_shapes_and_parameters() -> None:
    with pytest.raises(ValueError, match="same positive sample count"):
        paired_noninferiority([], [])
    with pytest.raises(ValueError, match="same positive sample count"):
        paired_noninferiority([1.0], [1.0, 2.0])
    with pytest.raises(ValueError, match="invalid bootstrap or margin"):
        paired_noninferiority([1.0], [1.0], bootstrap_samples=99)
    with pytest.raises(ValueError, match="invalid bootstrap or margin"):
        paired_noninferiority([1.0], [1.0], margin=1.0)
