import os

from locust import LoadTestShape


class SpikeShape(LoadTestShape):
    """Low baseline interrupted by short, sharp spikes at a fixed period.

    Robustness stage: tests whether GAT-GRU anticipates out-of-distribution
    jumps and whether PPO prioritises SLO over cost during a spike.
    """

    duration = int(os.environ.get("RUN_DURATION_SEC", 30 * 60))
    baseline_users = 25
    spike_users = 220
    spike_seconds = 90           # each spike lasts 90s
    interval_seconds = 360       # a new spike starts every 6 minutes
    spawn_rate_baseline = 5
    spawn_rate_spike = 40        # ramp into the spike fast

    def tick(self):
        run_time = self.get_run_time()
        if run_time >= self.duration:
            return None

        if run_time % self.interval_seconds < self.spike_seconds:
            return self.spike_users, self.spawn_rate_spike
        return self.baseline_users, self.spawn_rate_baseline
