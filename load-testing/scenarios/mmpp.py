"""MMPP - Markov Modulated Poisson Process (3 states).

A hidden continuous-time Markov chain switches between Idle / Normal / High.
Sojourn time in each state ~ Exponential(mean_sojourn); in state s sessions arrive as
Poisson(rate_s). Transitions follow the embedded chain below (no self-loops).

Purpose: flash sales / sudden spikes at random times - tests how fast the autoscaler
detects a state change. Unlike the Locust 'spike' scenario, timing and duration are random.
Per-state load: Idle ~9 rps, Normal ~21 rps, High ~90 rps (6 requests/session).
"""
CONFIG = {
    'model': 'mmpp',
    'states': {
        'idle':   {'rate': 1.5,  'mean_sojourn': 120.0},
        'normal': {'rate': 3.5,  'mean_sojourn': 180.0},
        'high':   {'rate': 15.0, 'mean_sojourn': 60.0},
    },
    'transitions': {
        'idle':   {'normal': 0.8, 'high': 0.2},
        'normal': {'idle': 0.4, 'high': 0.6},
        'high':   {'normal': 0.9, 'idle': 0.1},
    },
    'initial': 'normal',
    'mean_actions': 5,
    'think_mean_sec': 2.0,
}
