"""
locustfile.py - OPEN-model arrival driver for the mathematical workload scenarios.

Standard Locust users form a CLOSED loop (a user waits for the response before sending
again), so offered load drops when the system slows down. Here a single driver user
replays a pre-generated trace (arrival_models.build_trace): every planned arrival is
started at its scheduled time in its own greenlet, whether earlier requests have finished
or not. Each session / client has its own HttpSession (cookies -> cart, checkout work),
and every request is still recorded in Locust's statistics.

Run (from this directory, see run-scenario.sh):
    SCENARIO=mmpp SEED=1001 RUN_DURATION_SEC=1800 TRACE_DIR=results/mmpp/run_01 \
    locust -f locustfile.py --headless --users 1 --spawn-rate 1 --run-time 1800s --host http://...

Env: SCENARIO (nhpp|mmpp|pareto|onoff), SEED, RUN_DURATION_SEC, TRACE_DIR,
     MAX_CONCURRENT (default 400): cap on in-flight sessions/requests to protect the 1-vCPU
     load generator. Arrivals beyond the cap are DROPPED and counted in driver_report.json.
"""
import importlib
import json
import os
import random
import time
from datetime import datetime

import gevent
from gevent.pool import Pool
from locust import HttpUser, constant, events, task
from locust.clients import HttpSession

import arrival_models as am

SCENARIO = os.environ.get('SCENARIO', 'nhpp')
SEED = int(os.environ.get('SEED', '1'))
DURATION = int(os.environ.get('RUN_DURATION_SEC', 1800))
TRACE_DIR = os.environ.get('TRACE_DIR', os.path.join('results', SCENARIO, 'adhoc'))
MAX_CONCURRENT = int(os.environ.get('MAX_CONCURRENT', 400))
CONFIG = importlib.import_module(f'scenarios.{SCENARIO}').CONFIG

PRODUCT_IDS = ('0PUK6V6EV0', '1YMWWN1N4O', '2ZYFJ3GM2N', '66VCHSJNUP', '6E92ZMYYFZ',
               '9SIQT8TOJO', 'L9ECAV7KIM', 'LS4PSXUNUM', 'OLJCESPC7Z')
CURRENCIES = ('USD', 'EUR', 'CAD', 'JPY', 'GBP', 'TRY')
# Same action mix as load-testing/locustfile.py (browse-heavy, occasional checkout).
ACTIONS = (('browse', 12), ('product', 7), ('view_cart', 3), ('add', 3), ('checkout', 1), ('currency', 1))
CHECKOUT_FORM = {
    'email': 'math-shopper@example.com', 'street_address': '1600 Amphitheatre Parkway', 'zip_code': '94043',
    'city': 'Mountain View', 'state': 'CA', 'country': 'United States',
    'credit_card_number': '4111111111111111',  # Visa test number, passes Luhn 'credit_card_expiration_month': '12',
    'credit_card_expiration_year': str(datetime.now().year + 2), 'credit_card_cvv': '672',
}

STATS = {'planned': 0, 'started': 0, 'dropped': 0, 'completed': 0, 'failed': 0, 'max_in_flight': 0}


class Client:
    """One shopper with its own cookie jar (shop_session-id) and cart state."""

    def __init__(self, user, rng):
        self.http = HttpSession(base_url=user.host, request_event=user.environment.events.request, user=user)
        self.rng = rng
        self.cart_has_items = False

    def do(self, action):
        c, rng = self.http, self.rng
        if action == 'browse':
            c.get('/', name='GET /')
        elif action == 'product':
            c.get(f'/product/{rng.choice(PRODUCT_IDS)}', name='GET /product/[id]')
        elif action == 'view_cart':
            c.get('/cart', name='GET /cart')
        elif action == 'add':
            r = c.post('/cart', data={'product_id': rng.choice(PRODUCT_IDS), 'quantity': 1}, name='POST /cart')
            self.cart_has_items = self.cart_has_items or r.status_code < 400
        elif action == 'checkout':
            if not self.cart_has_items:            # an empty-cart checkout is not a real request
                return self.do('add')
            c.post('/cart/checkout', data=CHECKOUT_FORM, name='POST /cart/checkout')
            self.cart_has_items = False
        elif action == 'currency':
            c.post('/setCurrency', data={'currency_code': rng.choice(CURRENCIES)}, name='POST /setCurrency')

    def random_action(self):
        names, weights = zip(*ACTIONS)
        return self.rng.choices(names, weights=weights)[0]

    def think(self, mean):
        gevent.sleep(am.exponential(self.rng, mean))


class ArrivalDriver(HttpUser):
    """Exactly one instance (--users 1): replays the arrival trace in real time."""
    wait_time = constant(0)

    def _session(self, ev):
        rng = random.Random(SEED * 1_000_003 + ev['source'])
        client = Client(self, rng)
        think = CONFIG.get('think_mean_sec', 2.0)
        client.do('browse')
        if CONFIG['model'] == 'pareto':
            for _ in range(ev['size']):            # heavy-tailed work: S items then checkout
                client.think(think)
                client.do('add')
            client.think(think)
            client.do('view_cart')
            client.think(think)
            client.do('checkout')
        else:
            for _ in range(ev['size']):            # K ~ Geometric actions
                client.think(think)
                client.do(client.random_action())

    def _request(self, ev):
        client = self.clients.get(ev['source'])
        if client is None:
            client = self.clients[ev['source']] = Client(self, random.Random(SEED * 1_000_003 + ev['source']))
        client.do(client.random_action())

    def _run(self, ev):
        try:
            (self._session if ev['kind'] == 'session' else self._request)(ev)
            STATS['completed'] += 1
        except Exception:                          # network errors are already reported by HttpSession
            STATS['failed'] += 1

    @task
    def drive(self):
        trace = am.build_trace(CONFIG, DURATION, SEED)
        am.write_trace(trace, TRACE_DIR)
        STATS.update(expected_mean_rps=trace['expected_mean_rps'], designed_mean_rps=trace['designed_mean_rps'])
        self.clients = {}
        pool = Pool(MAX_CONCURRENT)
        t0 = time.monotonic()
        for ev in trace['events']:
            delay = ev['t'] - (time.monotonic() - t0)
            if delay > 0:
                gevent.sleep(delay)
            STATS['planned'] += 1
            if pool.full():
                STATS['dropped'] += 1
                continue
            STATS['started'] += 1
            pool.spawn(self._run, ev)
            STATS['max_in_flight'] = max(STATS['max_in_flight'], len(pool))
        while True:                                # trace exhausted: idle until --run-time stops the test
            gevent.sleep(30)


@events.test_stop.add_listener
def _report(environment, **_):
    STATS['dropped_ratio'] = STATS['dropped'] / STATS['planned'] if STATS['planned'] else 0.0
    STATS.update(scenario=SCENARIO, seed=SEED, duration=DURATION, max_concurrent=MAX_CONCURRENT)
    os.makedirs(TRACE_DIR, exist_ok=True)
    with open(os.path.join(TRACE_DIR, 'driver_report.json'), 'w') as f:
        json.dump(STATS, f, indent=1)
    if STATS['dropped']:
        print(f"[driver] WARN {STATS['dropped']} arrivals dropped (MAX_CONCURRENT={MAX_CONCURRENT}) - "
              f"load generator saturated, offered load lower than designed")
