from fs_diloco.observability.resource_monitor import ResourceMonitor, SystemCpuUtilizationReader


class SequenceReader:
    def __init__(self, values):
        self.values = iter(values)
        self.last = None

    def __call__(self):
        try:
            self.last = next(self.values)
        except StopIteration:
            pass
        return self.last


def test_resource_monitor_tracks_training_and_cycle_peaks():
    monitor = ResourceMonitor(
        cpu_utilization_reader=SequenceReader([10.0, 30.0, 20.0, 15.0, 25.0]),
        gpu_utilization_reader=SequenceReader([40.0, 70.0, 50.0, 60.0, 65.0]),
        sample_interval_seconds=60.0,
    )
    monitor.start()
    monitor.begin_cycle()
    monitor.record_step_duration(2.0)
    monitor.record_step_duration(4.0)
    first = monitor.cycle_snapshot()
    assert first["training_cpu_utilization_peak_percent"] == 30.0
    assert first["training_gpu_utilization_peak_percent"] == 70.0
    assert first["local_cycle_cpu_utilization_peak_percent"] == 30.0
    assert first["local_cycle_gpu_utilization_peak_percent"] == 70.0
    assert first["local_cycle_step_time_seconds_mean"] == 3.0

    monitor.begin_cycle()
    monitor.record_step_duration(1.0)
    second = monitor.cycle_snapshot()
    assert second["training_cpu_utilization_peak_percent"] == 30.0
    assert second["training_gpu_utilization_peak_percent"] == 70.0
    assert second["local_cycle_cpu_utilization_peak_percent"] == 25.0
    assert second["local_cycle_gpu_utilization_peak_percent"] == 65.0
    assert second["local_cycle_step_time_seconds_mean"] == 1.0
    monitor.stop()


def test_system_cpu_reader_uses_delta_between_proc_stat_samples(tmp_path):
    stat = tmp_path / "stat"
    stat.write_text("cpu  10 0 10 80 0 0 0 0\n", encoding="utf-8")
    reader = SystemCpuUtilizationReader(stat)
    assert reader() is None
    stat.write_text("cpu  20 0 20 160 0 0 0 0\n", encoding="utf-8")
    assert reader() == 20.0
