"""ON/OFF model - per-client bursts, heavy-tailed periods.

N independent clients (persistent sessions, keep their cookie/cart). Each client alternates:
  ON : sends requests as Poisson(rate_on) - a burst of activity
  OFF: silent ("thinking" / idle)
ON and OFF durations ~ Bounded Pareto. With 1 < alpha < 2 the superposition of many such
sources is (asymptotically) self-similar with Hurst H = (3 - min(alpha_on, alpha_off)) / 2
= 0.85 here (Taqqu, Willinger & Sherman 1997): bursty at every time scale, the burstiness
does not smooth out when aggregated - unlike Poisson (H = 0.5).

Purpose: realistic bursty client behaviour / east-west chatter; tests oscillation of the
autoscaler. Mean ON 13 s, OFF 31 s -> ~30% of sources ON -> ~27 rps on average.
"""
CONFIG = {
    'model': 'onoff',
    'n_sources': 60,
    'rate_on': 1.5,
    'on':  {'alpha': 1.5, 'low': 5.0,  'high': 300.0},
    'off': {'alpha': 1.3, 'low': 10.0, 'high': 600.0},
    'warmup_sec': 600,
}
