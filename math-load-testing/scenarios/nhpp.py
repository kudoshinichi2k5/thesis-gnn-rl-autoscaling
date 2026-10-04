"""NHPP - Non-Homogeneous Poisson Process.

Sessions arrive as a Poisson process whose rate varies smoothly in time:
    lambda(t) = base + amp1 * sin(2 pi t / T + phi) + amp2 * sin(4 pi t / T + 2 phi)   [sessions/s]
A "daily cycle" compressed into T = 15 min (two cycles per 30-min run); the second harmonic
makes rise and fall asymmetric. phi is drawn per run (seeded) so runs are not identical.
Each session: home page + K ~ Geometric(mean 5) actions, exponential think time.

Purpose: predictive autoscaling - load has trend/seasonality the forecaster can learn.
Range ~ 0.5 .. 13.5 sessions/s  ->  ~3 .. 80 rps at the frontend (mean ~42 rps).
"""
CONFIG = {
    'model': 'nhpp',
    'base': 7.0,
    'amp1': 5.0,
    'amp2': 1.5,
    'floor': 0.5,
    'period_sec': 900,
    'random_phase': True,
    'mean_actions': 5,
    'think_mean_sec': 2.0,
}
