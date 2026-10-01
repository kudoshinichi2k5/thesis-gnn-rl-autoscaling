from locust import LoadTestShape


class BurstyShape(LoadTestShape):
    """Repeated bursts to expose delayed scaling and replica oscillation."""

    duration = 10 * 60
    phase_seconds = 60
    low_users = 10
    high_users = 60

    def tick(self):
        run_time = self.get_run_time()
        if run_time >= self.duration:
            return None

        phase = int(run_time // self.phase_seconds)
        if phase % 2 == 0:
            return self.low_users, 10
        return self.high_users, 25
