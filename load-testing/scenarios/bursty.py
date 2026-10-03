import os
import random

from locust import LoadTestShape


class BurstyShape(LoadTestShape):
    """Irregular, randomly-timed bursts on top of a low baseline.

    Unlike spike, timing and amplitude are re-rolled every window, mimicking
    noisy flash-crowd traffic. Fine-tuning stage: GAT-GRU learns shifting
    service relationships; PPO learns to tell noise from real load and avoid
    thrashing.
    """

    duration = int(os.environ.get("RUN_DURATION_SEC", 30 * 60))
    baseline_users = 20
    burst_min_users = 80
    burst_max_users = 180
    window_seconds = 30          # re-roll the burst decision every 30s
    burst_probability = 0.35     # chance a window is a burst
    spawn_rate = 15

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # BURSTY_SEED makes a run reproducible; unset -> different bursts per run.
        seed = os.environ.get("BURSTY_SEED")
        self._rng = random.Random(int(seed) if seed else None)
        self._window = -1
        self._users = self.baseline_users

    def tick(self):
        run_time = self.get_run_time()
        if run_time >= self.duration:
            return None

        window = int(run_time // self.window_seconds)
        if window != self._window:
            self._window = window
            if self._rng.random() < self.burst_probability:
                self._users = self._rng.randint(self.burst_min_users, self.burst_max_users)
            else:
                self._users = max(1, int(self.baseline_users * self._rng.uniform(0.7, 1.3)))
        return self._users, self.spawn_rate
