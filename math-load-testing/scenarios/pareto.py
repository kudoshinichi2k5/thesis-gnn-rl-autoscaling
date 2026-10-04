"""Bounded Pareto - heterogeneous (heavy-tailed) workload.

Sessions arrive as a homogeneous Poisson process (constant rate), but the WORK of each
session is heavy-tailed: the number of products added to the cart before checkout is
    S = ceil(X),  X ~ BoundedPareto(alpha=1.2, L=1, H=40)
Most sessions buy 1-3 items, a few buy dozens. Checkout cost grows with S (checkoutservice
calls productcatalog/currency per item, cartservice/redis per item), so a few heavy sessions
can saturate CPU while the request COUNT stays flat ("80/20" effect).
Session: home -> S x add-to-cart -> view cart -> checkout.

Mapping note: Online Boutique has no arbitrary "heavy request"; work size is expressed as
items per order, the most expensive dimension the application exposes.
E[ceil(X)] ~ 3.8 items -> ~6.8 requests/session -> ~20 rps at 3 sessions/s.
"""
CONFIG = {
    'model': 'pareto',
    'session_rate': 3.0,
    'items': {'alpha': 1.2, 'low': 1.0, 'high': 40.0},
    'think_mean_sec': 1.0,
}
