import os
import random

from locust import LoadTestShape


class NormalShape(LoadTestShape):
    """Steady baseline: linear warm-up, then +/-10% jitter around the baseline.

    Training stage 1 (initialisation): GAT-GRU learns the service-graph
    structure and normal temporal trend; PPO learns to hold steady at low load.
    """

    duration = int(os.environ.get("RUN_DURATION_SEC", 30 * 60))
    baseline_users = 40
    warmup_seconds = 60
    jitter = 0.10
    spawn_rate = 5

    def tick(self):
        run_time = self.get_run_time()
        if run_time >= self.duration:
            return None

        if run_time < self.warmup_seconds:
            users = int(self.baseline_users * run_time / self.warmup_seconds)
        else:
            users = int(self.baseline_users * (1 + random.uniform(-self.jitter, self.jitter)))
        return max(1, users), self.spawn_rate
