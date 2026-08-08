"""Durable-looking fake PBS port for scheduler state-machine tests."""

from __future__ import annotations

from collections import defaultdict, deque
from typing import Any

from fs_diloco.runtime.pbs_scheduler import PBSJobObservation


class FakePBS:
    def __init__(self) -> None:
        self._queries: dict[tuple[str, bool], deque[PBSJobObservation]] = defaultdict(deque)
        self._requests: dict[str, PBSJobObservation] = {}
        self.submissions: list[dict[str, Any]] = []

    def queue_query(
        self,
        job_id: str,
        observation: PBSJobObservation,
        *,
        historical: bool = False,
    ) -> None:
        self._queries[(job_id, historical)].append(observation)

    def query(self, job_id: str, *, historical: bool = False) -> PBSJobObservation:
        queued = self._queries[(job_id, historical)]
        if queued:
            return queued.popleft()
        return PBSJobObservation(job_id, "no_record", None, 1, "not queued in FakePBS")

    def bind_request(self, request_id: str, observation: PBSJobObservation) -> None:
        self._requests[request_id] = observation

    def find_by_launch_request(self, request_id: str) -> PBSJobObservation | None:
        return self._requests.get(request_id)

    def submit_learner(self, **kwargs: Any) -> dict[str, Any]:
        self.submissions.append(dict(kwargs))
        return {"returncode": 0, "job_id_raw": f"fake-{len(self.submissions)}.opbs"}
