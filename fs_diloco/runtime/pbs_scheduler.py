"""Small auditable PBS query/submission adapter for recovery candidates."""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any


TERMINAL_STATES = {"F", "X"}
OUTSTANDING_CLASSIFICATIONS = {
    "queued",
    "prologue",
    "running",
    "suspended",
    "submission_unknown",
}


def normalize_job_id(value: str) -> str:
    stripped = value.strip()
    if not stripped:
        raise ValueError("empty PBS job ID")
    return stripped.split(".", 1)[0]


def parse_qstat_full(output: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    current_key: str | None = None
    for raw_line in output.splitlines():
        if raw_line.startswith(("\t", " ")) and current_key is not None and " = " not in raw_line:
            fields[current_key] += raw_line.strip()
            continue
        match = re.match(r"^\s*([^=]+?)\s*=\s*(.*)$", raw_line)
        if match:
            current_key = match.group(1).strip()
            fields[current_key] = match.group(2).strip()
    return fields


def parse_qstat_jobs(output: str) -> list[tuple[str, dict[str, str]]]:
    matches = list(re.finditer(r"(?m)^Job Id:\s*(\S+)\s*$", output))
    jobs: list[tuple[str, dict[str, str]]] = []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(output)
        jobs.append((match.group(1), parse_qstat_full(output[match.end() : end])))
    return jobs


def classify_scheduler_state(fields: dict[str, str] | None) -> str:
    if fields is None:
        return "query_failed"
    if not fields:
        return "no_record"
    state = fields.get("job_state", "").upper()
    try:
        substate = int(fields.get("substate", ""))
    except ValueError:
        substate = None
    if state in TERMINAL_STATES:
        return "finished"
    if state in {"Q", "H", "W"}:
        return "queued"
    if state == "S":
        return "suspended"
    if state == "R" and substate is not None and substate < 42:
        return "prologue"
    if state in {"R", "E", "B"}:
        return "running"
    return "unknown"


@dataclass(frozen=True)
class PBSJobObservation:
    job_id: str
    classification: str
    fields: dict[str, str] | None
    returncode: int
    stderr: str

    @property
    def outstanding(self) -> bool:
        return self.classification in OUTSTANDING_CLASSIFICATIONS


class PBSScheduler:
    def __init__(
        self,
        *,
        qsub_binary: str = "qsub",
        qstat_binary: str = "qstat",
        timeout_seconds: float = 30.0,
    ) -> None:
        self.qsub_binary = qsub_binary
        self.qstat_binary = qstat_binary
        self.timeout_seconds = float(timeout_seconds)

    def query(self, job_id: str, *, historical: bool = False) -> PBSJobObservation:
        command = [self.qstat_binary]
        if historical:
            command.append("-H")
        command.extend(("-f", job_id))
        try:
            completed = subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
                timeout=self.timeout_seconds,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return PBSJobObservation(
                job_id=normalize_job_id(job_id),
                classification="query_failed",
                fields=None,
                returncode=-1,
                stderr=repr(exc),
            )
        fields = parse_qstat_full(completed.stdout) if completed.returncode == 0 else None
        return PBSJobObservation(
            job_id=normalize_job_id(job_id),
            classification=classify_scheduler_state(fields),
            fields=fields,
            returncode=completed.returncode,
            stderr=completed.stderr.strip(),
        )

    def submit_candidate(
        self,
        *,
        script: str | Path,
        request_fingerprint: str,
        shared_root: str | Path,
        descriptor_sha256: str,
        walltime: str,
    ) -> dict[str, Any]:
        if not request_fingerprint or any(
            character in request_fingerprint for character in ",=\n\r"
        ):
            raise ValueError("request_fingerprint contains an unsafe PBS variable character")
        variables = ",".join(
            (
                f"FS_DILOCO_SHARED_ROOT={Path(shared_root).resolve()}",
                f"FS_DILOCO_RECOVERY_REQUEST={request_fingerprint}",
                f"FS_DILOCO_EXPECTED_DESCRIPTOR_SHA256={descriptor_sha256}",
            )
        )
        name = f"diloco_s_{request_fingerprint[-10:]}"
        command = [
            self.qsub_binary,
            "-N",
            name,
            "-l",
            f"walltime={walltime}",
            "-v",
            variables,
            str(Path(script)),
        ]
        try:
            completed = subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
                timeout=self.timeout_seconds,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return {
                "returncode": -1,
                "stdout": "",
                "stderr": repr(exc),
                "job_name": name,
                "request_fingerprint": request_fingerprint,
            }
        stdout = completed.stdout.strip()
        result: dict[str, Any] = {
            "returncode": completed.returncode,
            "stdout": stdout,
            "stderr": completed.stderr.strip(),
            "job_name": name,
            "request_fingerprint": request_fingerprint,
        }
        if completed.returncode == 0 and stdout:
            raw = stdout.splitlines()[-1]
            result["job_id_raw"] = raw
            result["job_id_normalized"] = normalize_job_id(raw)
        return result

    def find_by_request_fingerprint(self, request_fingerprint: str) -> PBSJobObservation | None:
        try:
            completed = subprocess.run(
                [self.qstat_binary, "-f"],
                check=False,
                capture_output=True,
                text=True,
                timeout=self.timeout_seconds,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        if completed.returncode != 0:
            return None
        marker = f"FS_DILOCO_RECOVERY_REQUEST={request_fingerprint}"
        for job_id, fields in parse_qstat_jobs(completed.stdout):
            if marker not in fields.get("Variable_List", ""):
                continue
            return PBSJobObservation(
                job_id=normalize_job_id(job_id),
                classification=classify_scheduler_state(fields),
                fields=fields,
                returncode=0,
                stderr="",
            )
        return None
