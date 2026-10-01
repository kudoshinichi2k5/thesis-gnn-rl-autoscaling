from locust import LoadTestShape


class NormalShape(LoadTestShape):
    """Steady baseline for comparing resource use and scaling latency."""

    duration = 10 * 60
    users = 20
    spawn_rate = 1

    def tick(self):
        if self.get_run_time() >= self.duration:
            return None
        return self.users, self.spawn_rate
