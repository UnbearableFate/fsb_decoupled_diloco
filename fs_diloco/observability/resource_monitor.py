"""Low-overhead learner CPU/GPU utilization sampling."""

from __future__ import annotations

import math
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any


UtilizationReader = Callable[[], float | None]


def _bounded_percent(value: float | None) -> float | None:
    if value is None:
        return None
    numeric = float(value)
    if not math.isfinite(numeric):
        return None
    return min(100.0, max(0.0, numeric))


class SystemCpuUtilizationReader:
    """Read whole-node CPU utilization from ``/proc/stat`` without dependencies."""

    def __init__(self, stat_path: str | Path = "/proc/stat") -> None:
        self.stat_path = Path(stat_path)
        self._previous: tuple[int, int] | None = None

    def __call__(self) -> float | None:
        first_line = self.stat_path.read_text(encoding="utf-8").splitlines()[0]
        fields = first_line.split()
        if not fields or fields[0] != "cpu":
            return None
        values = [int(value) for value in fields[1:]]
        if len(values) < 4:
            return None
        idle = values[3] + (values[4] if len(values) > 4 else 0)
        total = sum(values)
        previous = self._previous
        self._previous = (total, idle)
        if previous is None:
            return None
        total_delta = total - previous[0]
        idle_delta = idle - previous[1]
        if total_delta <= 0:
            return None
        return 100.0 * (total_delta - idle_delta) / total_delta


class ResourceMonitor:
    """Sample resources in the background and track training/cycle peaks."""

    def __init__(
        self,
        *,
        gpu_utilization_reader: UtilizationReader | None = None,
        cpu_utilization_reader: UtilizationReader | None = None,
        sample_interval_seconds: float = 1.0,
    ) -> None:
        self._cpu_reader = cpu_utilization_reader or SystemCpuUtilizationReader()
        self._gpu_reader = gpu_utilization_reader
        self._sample_interval_seconds = max(0.1, float(sample_interval_seconds))
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._training_cpu_peak: float | None = None
        self._training_gpu_peak: float | None = None
        self._training_sample_count = 0
        self._cycle_cpu_peak: float | None = None
        self._cycle_gpu_peak: float | None = None
        self._cycle_sample_count = 0
        self._cycle_step_seconds_total = 0.0
        self._cycle_step_count = 0
        self._reader_errors = 0

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop_event.clear()
        self.sample_now()
        self._thread = threading.Thread(
            target=self._sample_loop,
            name="learner-resource-monitor",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=max(1.0, 2.0 * self._sample_interval_seconds))
        self._thread = None
        self.sample_now()

    def begin_cycle(self) -> None:
        with self._lock:
            self._cycle_cpu_peak = None
            self._cycle_gpu_peak = None
            self._cycle_sample_count = 0
            self._cycle_step_seconds_total = 0.0
            self._cycle_step_count = 0
        self.sample_now()

    def record_step_duration(self, seconds: float) -> None:
        numeric = float(seconds)
        if math.isfinite(numeric) and numeric >= 0.0:
            with self._lock:
                self._cycle_step_seconds_total += numeric
                self._cycle_step_count += 1
        self.sample_now()

    def sample_now(self) -> None:
        try:
            cpu = _bounded_percent(self._cpu_reader())
        except Exception:
            cpu = None
            with self._lock:
                self._reader_errors += 1
        try:
            gpu = _bounded_percent(self._gpu_reader()) if self._gpu_reader is not None else None
        except Exception:
            gpu = None
            with self._lock:
                self._reader_errors += 1

        if cpu is None and gpu is None:
            return
        with self._lock:
            self._training_sample_count += 1
            self._cycle_sample_count += 1
            if cpu is not None:
                self._training_cpu_peak = (
                    cpu if self._training_cpu_peak is None else max(self._training_cpu_peak, cpu)
                )
                self._cycle_cpu_peak = (
                    cpu if self._cycle_cpu_peak is None else max(self._cycle_cpu_peak, cpu)
                )
            if gpu is not None:
                self._training_gpu_peak = (
                    gpu if self._training_gpu_peak is None else max(self._training_gpu_peak, gpu)
                )
                self._cycle_gpu_peak = (
                    gpu if self._cycle_gpu_peak is None else max(self._cycle_gpu_peak, gpu)
                )

    def cycle_snapshot(self) -> dict[str, float | int | None]:
        with self._lock:
            step_mean = (
                self._cycle_step_seconds_total / self._cycle_step_count
                if self._cycle_step_count
                else None
            )
            return {
                "training_cpu_utilization_peak_percent": self._training_cpu_peak,
                "training_gpu_utilization_peak_percent": self._training_gpu_peak,
                "local_cycle_cpu_utilization_peak_percent": self._cycle_cpu_peak,
                "local_cycle_gpu_utilization_peak_percent": self._cycle_gpu_peak,
                "local_cycle_step_time_seconds_mean": step_mean,
                "local_cycle_step_count": self._cycle_step_count,
                "local_cycle_resource_sample_count": self._cycle_sample_count,
            }

    def training_snapshot(self) -> dict[str, float | int | None]:
        with self._lock:
            return {
                "training_cpu_utilization_peak_percent": self._training_cpu_peak,
                "training_gpu_utilization_peak_percent": self._training_gpu_peak,
                "training_resource_sample_count": self._training_sample_count,
                "resource_reader_error_count": self._reader_errors,
            }

    def _sample_loop(self) -> None:
        while not self._stop_event.wait(self._sample_interval_seconds):
            self.sample_now()


def finite_resource_metrics(payload: dict[str, Any]) -> dict[str, float | int]:
    """Return JSON-safe finite resource metrics from a monitor snapshot."""

    result: dict[str, float | int] = {}
    for key, value in payload.items():
        if value is None:
            continue
        if isinstance(value, int):
            result[key] = value
            continue
        numeric = float(value)
        if math.isfinite(numeric):
            result[key] = numeric
    return result
