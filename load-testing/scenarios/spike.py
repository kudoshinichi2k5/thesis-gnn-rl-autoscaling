from locust import LoadTestShape


class SpikeShape(LoadTestShape):
    """Sharp demand jump for comparing proactive scaling with reactive HPA."""

    baseline_seconds = 3 * 60
    spike_ramp_seconds = 5
    spike_hold_seconds = 3 * 60
    recovery_ramp_seconds = 5
    duration = 10 * 60

    def tick(self):
        run_time = self.get_run_time()

        if run_time < self.baseline_seconds:
            return 10, 2
        if run_time < self.baseline_seconds + self.spike_ramp_seconds:
            return 100, 18
        if run_time < self.baseline_seconds + self.spike_ramp_seconds + self.spike_hold_seconds:
            return 100, 1
        if run_time < self.baseline_seconds + self.spike_ramp_seconds + self.spike_hold_seconds + self.recovery_ramp_seconds:
            return 10, 18
        if run_time < self.duration:
            return 10, 1
        return None
