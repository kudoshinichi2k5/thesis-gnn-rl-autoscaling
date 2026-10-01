import random
from datetime import datetime

from locust import HttpUser, between, task


PRODUCT_IDS = (
    "0PUK6V6EV0",
    "1YMWWN1N4O",
    "2ZYFJ3GM2N",
    "66VCHSJNUP",
    "6E92ZMYYFZ",
    "9SIQT8TOJO",
    "L9ECAV7KIM",
    "LS4PSXUNUM",
    "OLJCESPC7Z",
)
CURRENCIES = ("USD", "EUR", "CAD", "JPY", "GBP", "TRY")


class OnlineBoutiqueShopper(HttpUser):
    """A lightweight browser-like user for the Online Boutique frontend."""

    wait_time = between(1, 3)

    def on_start(self):
        self.cart_has_items = False
        self.client.get("/", name="GET /")

    @task(12)
    def browse_home(self):
        self.client.get("/", name="GET /")

    @task(7)
    def view_product(self):
        product_id = random.choice(PRODUCT_IDS)
        self.client.get(f"/product/{product_id}", name="GET /product/[id]")

    @task(3)
    def view_cart(self):
        self.client.get("/cart", name="GET /cart")

    @task(3)
    def add_product_to_cart(self):
        product_id = random.choice(PRODUCT_IDS)
        response = self.client.post(
            "/cart",
            data={"product_id": product_id, "quantity": random.choice((1, 1, 2))},
            name="POST /cart",
        )
        if response.status_code < 400:
            self.cart_has_items = True

    @task(1)
    def checkout(self):
        if not self.cart_has_items:
            return

        self.client.post(
            "/cart/checkout",
            data={
                "email": "locust-shopper@example.com",
                "street_address": "1600 Amphitheatre Parkway",
                "zip_code": "94043",
                "city": "Mountain View",
                "state": "CA",
                "country": "United States",
                "credit_card_number": "4434434434434431",
                "credit_card_expiration_month": "12",
                "credit_card_expiration_year": str(datetime.now().year + 2),
                "credit_card_cvv": "672",
            },
            name="POST /cart/checkout",
        )
        self.cart_has_items = False

    @task(1)
    def change_currency(self):
        currency_code = random.choice(CURRENCIES)
        self.client.post(
            "/setCurrency",
            data={"currency_code": currency_code},
            name="POST /setCurrency",
        )
