from unittest.mock import Mock, patch

from django.test import Client, TestCase, override_settings
from django.utils import timezone

from .models import DiscordWebhook, Monitor, Product, SystemState, User
from .services import AppleCanadaAdapter, AdapterError, BestBuyCanadaAdapter, NintendoCanadaAdapter, decrypt, encrypt


@override_settings(WEBHOOK_ENCRYPTION_KEY="HsdQznbn2wz1LikNoUvwzmgskkODlG5pgvAd1uKVXpQ=")
class AccountAndMonitorTests(TestCase):
    def test_login_returns_csrf_token_for_authenticated_mutations(self):
        client = Client(enforce_csrf_checks=True)
        initial = client.get("/api/v1/")
        register = client.post(
            "/api/v1/register/",
            data={"email": "admin@example.com", "password": "A-secure-passphrase-123", "displayName": "Admin"},
            content_type="application/json",
            headers={"X-CSRFToken": initial.json()["csrfToken"]},
        )
        self.assertEqual(register.status_code, 201)
        session = client.get("/api/v1/")
        login = client.post(
            "/api/v1/login/",
            data={"email": "admin@example.com", "password": "A-secure-passphrase-123"},
            content_type="application/json",
            headers={"X-CSRFToken": session.json()["csrfToken"]},
        )
        self.assertEqual(login.status_code, 200)
        webhook = client.post(
            "/api/v1/webhooks/",
            data={"name": "Discord", "url": "https://discord.com/api/webhooks/1/token"},
            content_type="application/json",
            headers={"X-CSRFToken": login.json()["csrfToken"]},
        )
        self.assertEqual(webhook.status_code, 201)

    @override_settings(WEBHOOK_ENCRYPTION_KEY="ordinary-deployment-secret")
    def test_webhook_encryption_accepts_regular_secret(self):
        self.assertEqual(decrypt(encrypt("https://discord.com/api/webhooks/1/token")), "https://discord.com/api/webhooks/1/token")

    def register(self, email):
        return self.client.post("/api/v1/register/", data={"email": email, "password": "A-secure-passphrase-123", "displayName": "Test"}, content_type="application/json")

    def test_first_account_becomes_administrator(self):
        response = self.register("admin@example.com")
        user = User.objects.get(email="admin@example.com")
        self.assertEqual(response.status_code, 201)
        self.assertTrue(response.json()["approved"])
        self.assertTrue(user.is_superuser)
        self.assertEqual(user.status, User.Status.APPROVED)
        self.assertTrue(SystemState.objects.get(pk=1).initial_admin_claimed)

    def test_later_account_requires_approval(self):
        self.register("admin@example.com")
        response = self.register("member@example.com")
        member = User.objects.get(email="member@example.com")
        self.assertFalse(response.json()["approved"])
        self.assertFalse(member.is_active)
        self.assertEqual(member.status, User.Status.PENDING)
        login = self.client.post("/api/v1/login/", data={"email": member.email, "password": "A-secure-passphrase-123"}, content_type="application/json")
        self.assertEqual(login.status_code, 403)

    def test_staff_can_approve_pending_user(self):
        self.register("admin@example.com")
        self.register("member@example.com")
        self.client.post("/api/v1/login/", data={"email": "admin@example.com", "password": "A-secure-passphrase-123"}, content_type="application/json")
        member = User.objects.get(email="member@example.com")
        response = self.client.patch(f"/api/v1/staff-users/{member.id}/", data={"action": "approve"}, content_type="application/json")
        member.refresh_from_db()
        self.assertEqual(response.status_code, 200)
        self.assertTrue(member.is_active)
        self.assertEqual(member.status, User.Status.APPROVED)

    def test_monitor_is_scoped_to_its_owner(self):
        admin = User.objects.create_user(email="admin@example.com", password="A-secure-passphrase-123", status=User.Status.APPROVED, is_active=True)
        other = User.objects.create_user(email="other@example.com", password="A-secure-passphrase-123", status=User.Status.APPROVED, is_active=True)
        product = Product.objects.create(canonical_url="https://www.nintendo.com/en-ca/store/products/example", title="Example")
        webhook = DiscordWebhook.objects.create(owner=admin, name="Discord", encrypted_url=encrypt("https://discord.com/api/webhooks/1/token"))
        Monitor.objects.create(owner=admin, product=product, webhook=webhook)
        self.client.force_login(other)
        response = self.client.get("/api/v1/monitors/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["monitors"], [])

    def test_owner_can_remove_only_their_monitor(self):
        owner = User.objects.create_user(email="owner@example.com", password="A-secure-passphrase-123", status=User.Status.APPROVED, is_active=True)
        other = User.objects.create_user(email="other@example.com", password="A-secure-passphrase-123", status=User.Status.APPROVED, is_active=True)
        product = Product.objects.create(canonical_url="https://www.nintendo.com/en-ca/store/products/example", title="Example")
        owner_webhook = DiscordWebhook.objects.create(owner=owner, name="Owner Discord", encrypted_url=encrypt("https://discord.com/api/webhooks/1/token"))
        other_webhook = DiscordWebhook.objects.create(owner=other, name="Other Discord", encrypted_url=encrypt("https://discord.com/api/webhooks/2/token"))
        owner_monitor = Monitor.objects.create(owner=owner, product=product, webhook=owner_webhook)
        other_monitor = Monitor.objects.create(owner=other, product=product, webhook=other_webhook)

        self.client.force_login(other)
        self.assertEqual(self.client.delete(f"/api/v1/monitors/{owner_monitor.id}/").status_code, 404)
        self.assertTrue(Monitor.objects.filter(id=owner_monitor.id).exists())

        self.client.force_login(owner)
        self.assertEqual(self.client.delete(f"/api/v1/monitors/{owner_monitor.id}/").status_code, 200)
        self.assertFalse(Monitor.objects.filter(id=owner_monitor.id).exists())
        self.assertTrue(Monitor.objects.filter(id=other_monitor.id).exists())


class NintendoAdapterTests(TestCase):
    def fetch_snapshot(self, availability):
        html = f'''<script type="application/ld+json">{{"@context":"https://schema.org/","@graph":[{{"@type":["Product"],"name":"Nintendo Switch 2 Example","image":"https://images.example/product.jpg","offers":{{"price":"69.99","priceCurrency":"CAD","availability":"https://schema.org/{availability}"}}}}]}}</script>'''
        response = Mock(status_code=200, text=html)
        with patch("core.services.requests.get", return_value=response):
            return NintendoCanadaAdapter().validate("https://www.nintendo.com/en-ca/store/products/nintendo-switch-2-camera-123682/")

    def test_nested_json_ld_in_stock_is_available(self):
        snapshot = self.fetch_snapshot("InStock")
        self.assertEqual(snapshot.availability, "available")
        self.assertEqual(snapshot.price, "$69.99 CAD")

    def test_nested_json_ld_out_of_stock_is_unavailable(self):
        snapshot = self.fetch_snapshot("OutOfStock")
        self.assertEqual(snapshot.availability, "unavailable")


class BestBuyAdapterTests(TestCase):
    def test_best_buy_nearby_stores_exposes_pickup_location_ids(self):
        response = Mock(status_code=200, json=lambda: {"locations": [{"locationId": "928", "name": "Ottawa West", "city": "Nepean", "region": "ON", "distance": 10.44, "qpu": {"pickupOptions": ["IN_STORE_PICKUP"]}}, {"locationId": "858", "name": "Airport Kiosk", "qpu": {"pickupOptions": []}}]})
        response.raise_for_status = Mock()
        with patch("core.services.requests.get", return_value=response):
            stores = BestBuyCanadaAdapter().nearby_stores("A1A1A1")
        self.assertEqual(stores, [{"id": "928", "name": "Ottawa West", "city": "Nepean", "region": "ON", "distance": 10.44, "pickup": True}])

    def test_best_buy_shipping_purchasable_is_available(self):
        product_response = Mock(status_code=200, json=lambda: {"name": "Nintendo Switch 2 Console", "thumbnailImage": "https://images.example/switch.jpg", "salePrice": 679.99, "availability": {"isAvailableOnline": True, "onlineAvailability": "InStock"}})
        with patch("core.services.requests.get", return_value=product_response):
            snapshot = BestBuyCanadaAdapter().validate("https://www.bestbuy.ca/en-ca/product/nintendo-switch-2-console/19296507", "J9H0H8")
        self.assertEqual(snapshot.availability, "available")
        self.assertEqual(snapshot.title, "Nintendo Switch 2 Console")
        self.assertEqual(snapshot.price, "$679.99 CAD")

    def test_best_buy_shipping_not_purchasable_is_unavailable(self):
        product_response = Mock(status_code=200, json=lambda: {"name": "Nintendo Switch 2 Console", "availability": {"isAvailableOnline": False, "onlineAvailability": "OutOfStock"}})
        with patch("core.services.requests.get", return_value=product_response):
            snapshot = BestBuyCanadaAdapter().validate("https://www.bestbuy.ca/en-ca/product/nintendo-switch-2-console/19296507", "J9H0H8")
        self.assertEqual(snapshot.availability, "unavailable")

    def test_best_buy_selected_pickup_store_is_available(self):
        product_response = Mock(status_code=200, json=lambda: {"name": "Nintendo Switch 2 Console", "availability": {"isAvailableOnline": False, "onlineAvailability": "OutOfStock"}})
        pickup_response = Mock(status_code=200, json=lambda: {"availabilities": [{"pickup": {"locations": [{"locationKey": "928", "hasInventory": True, "isReservable": True}]}}]})
        product = Product(canonical_url="https://www.bestbuy.ca/en-ca/product/nintendo-switch-2-console/19296507", title="Nintendo Switch 2 Console")
        with patch("core.services.requests.get", side_effect=[product_response, pickup_response]) as request_get:
            snapshot = BestBuyCanadaAdapter().check(product, "A1A1A1", "pickup", ["928"])
        self.assertEqual(snapshot.availability, "available")
        self.assertEqual(request_get.call_args_list[1].kwargs["params"]["locations"], "928")


class AppleAdapterTests(TestCase):
    pickup_available_response = {"body": {"stores": [{"partsAvailability": {"MG854VC/A": {"pickupDisplay": "available"}}}]}}

    @override_settings(STOCKBOT_USER_AGENT="StockBot/1.0 (contact: ops@example.com)")
    def test_apple_fulfillment_uses_configured_user_agent(self):
        response = Mock(status_code=200, json=lambda: self.pickup_available_response)
        response.raise_for_status = Mock()
        with patch("core.services.requests.get", return_value=response) as request_get:
            AppleCanadaAdapter().validate("https://www.apple.com/ca/shop/", "A1A1A1", "pickup", external_id="MG854VC/A")
        self.assertEqual(request_get.call_args.kwargs["headers"]["User-Agent"], "StockBot/1.0 (contact: ops@example.com)")

    def test_apple_fulfillment_uses_current_endpoint_and_postal_code(self):
        response = Mock(status_code=200, json=lambda: self.pickup_available_response)
        response.raise_for_status = Mock()
        with patch("core.services.requests.get", return_value=response) as request_get:
            snapshot = AppleCanadaAdapter().validate("https://www.apple.com/ca/shop/", "A1A1A1", "pickup", external_id="MG854VC/A")
        self.assertEqual(snapshot.availability, "available")
        self.assertEqual(request_get.call_args.args[0], "https://www.apple.com/ca/shop/retail/pickup-message")
        self.assertEqual(request_get.call_args.kwargs["params"]["location"], "A1A1A1")
        self.assertEqual(request_get.call_args.kwargs["params"]["pl"], "true")
        self.assertEqual(request_get.call_args.kwargs["params"]["parts.0"], "MG854VC/A")
        self.assertNotIn("fae", request_get.call_args.kwargs["params"])
        self.assertNotIn("mts.0", request_get.call_args.kwargs["params"])
        self.assertNotIn("Cookie", request_get.call_args.kwargs["headers"])

    def test_apple_fulfillment_is_unavailable_when_no_store_has_pickup_stock(self):
        response = Mock(status_code=200, json=lambda: {"body": {"stores": [{"partsAvailability": {"MG854VC/A": {"pickupDisplay": "unavailable"}}}]}})
        response.raise_for_status = Mock()
        with patch("core.services.requests.get", return_value=response):
            snapshot = AppleCanadaAdapter().validate("https://www.apple.com/ca/shop/", "A1A1A1", "pickup", external_id="MG854VC/A")
        self.assertEqual(snapshot.availability, "unavailable")

    def test_apple_fulfillment_uses_only_selected_store_ids(self):
        response = Mock(
            status_code=200,
            json=lambda: {
                "body": {
                    "stores": [
                        {"storeNumber": "R001", "partsAvailability": {"MG854VC/A": {"pickupDisplay": "available"}}},
                        {"storeNumber": "R002", "partsAvailability": {"MG854VC/A": {"pickupDisplay": "unavailable"}}},
                    ]
                }
            },
        )
        response.raise_for_status = Mock()
        with patch("core.services.requests.get", return_value=response):
            snapshot = AppleCanadaAdapter().validate("https://www.apple.com/ca/shop/", "A1A1A1", "pickup", ["R002"], "MG854VC/A")
        self.assertEqual(snapshot.availability, "unavailable")

    def test_apple_nearby_stores_exposes_apple_store_ids(self):
        response = Mock(
            status_code=200,
            json=lambda: {
                "body": {
                    "stores": [
                        {"storeNumber": "R001", "storeName": "Rideau", "city": "Ottawa", "state": "ON", "distance": 2.4},
                    ]
                }
            },
        )
        response.raise_for_status = Mock()
        with patch("core.services.requests.get", return_value=response):
            stores = AppleCanadaAdapter().nearby_stores("A1A1A1", "MG854VC/A")
        self.assertEqual(stores, [{"id": "R001", "name": "Rideau", "city": "Ottawa", "region": "ON", "distance": 2.4, "pickup": True}])

    def test_apple_nearby_stores_supports_nested_retail_store_data(self):
        response = Mock(
            status_code=200,
            json=lambda: {
                "body": {
                    "stores": [
                        {
                            "storedistance": 3.1,
                            "retailStore": {
                                "storeNumber": "R002",
                                "name": "Bayshore",
                                "address": {"city": "Ottawa", "state": "ON"},
                            },
                        }
                    ]
                }
            },
        )
        response.raise_for_status = Mock()
        with patch("core.services.requests.get", return_value=response):
            stores = AppleCanadaAdapter().nearby_stores("A1A1A1", "MG854VC/A")
        self.assertEqual(stores, [{"id": "R002", "name": "Bayshore", "city": "Ottawa", "region": "ON", "distance": 3.1, "pickup": True}])

    def test_apple_fulfillment_uses_root_parts_availability(self):
        response = Mock(
            status_code=200,
            json=lambda: {"body": {"stores": [], "partsAvailability": {"MG854VC/A": {"pickupDisplay": "available"}}}},
        )
        response.raise_for_status = Mock()
        with patch("core.services.requests.get", return_value=response):
            snapshot = AppleCanadaAdapter().validate("https://www.apple.com/ca/shop/", "A1A1A1", "pickup", external_id="MG854VC/A")
        self.assertEqual(snapshot.availability, "available")

    def test_apple_shipping_uses_current_fulfillment_request(self):
        response = Mock(
            status_code=200,
            json=lambda: {
                "body": {
                    "content": {
                        "deliveryMessage": {
                            "MG854VC/A": {
                                "regular": {
                                    "deliveryOptions": [{"deliveryDate": "Tomorrow"}]
                                }
                            }
                        }
                    }
                }
            },
        )
        response.raise_for_status = Mock()
        with patch("core.services.requests.get", return_value=response) as request_get:
            snapshot = AppleCanadaAdapter().validate("https://www.apple.com/ca/shop/", "A1A1A1", "shipping", external_id="MG854VC/A")
        self.assertEqual(snapshot.availability, "available")
        self.assertEqual(request_get.call_args.args[0], "https://www.apple.com/ca/shop/delivery-message")
        self.assertEqual(request_get.call_args.kwargs["params"], {"mt": "regular", "parts.0": "MG854VC/A"})

    def test_apple_either_uses_pickup_when_shipping_is_unavailable(self):
        delivery_response = Mock(
            status_code=200,
            json=lambda: {
                "body": {
                    "content": {
                        "deliveryMessage": {
                            "MG854VC/A": {"regular": {"deliveryOptions": []}}
                        }
                    }
                }
            },
        )
        pickup_response = Mock(status_code=200, json=lambda: self.pickup_available_response)
        delivery_response.raise_for_status = Mock()
        pickup_response.raise_for_status = Mock()
        with patch("core.services.requests.get", side_effect=[pickup_response, delivery_response]) as request_get:
            snapshot = AppleCanadaAdapter().validate("https://www.apple.com/ca/shop/", "A1A1A1", "either", external_id="MG854VC/A")
        self.assertEqual(snapshot.availability, "available")
        self.assertEqual(request_get.call_count, 2)

    def test_apple_either_uses_pickup_when_delivery_is_blocked(self):
        pickup_response = Mock(status_code=200, json=lambda: self.pickup_available_response)
        delivery_response = Mock(status_code=541, text="Apple Shield")
        pickup_response.raise_for_status = Mock()
        with patch("core.services.requests.get", side_effect=[pickup_response, delivery_response]) as request_get:
            snapshot = AppleCanadaAdapter().validate("https://www.apple.com/ca/shop/", "A1A1A1", "either", external_id="MG854VC/A")
        self.assertEqual(snapshot.availability, "available")
        self.assertEqual(request_get.call_count, 2)

    def test_apple_pickup_requires_postal_code(self):
        with self.assertRaisesRegex(AdapterError, "requires a Canadian postal code"):
            AppleCanadaAdapter().validate("https://www.apple.com/ca/shop/", fulfillment="pickup", external_id="MG854VC/A")

    def test_apple_fulfillment_logs_block_response(self):
        response = Mock(status_code=403, text="Access Denied")
        with patch("core.services.requests.get", return_value=response), self.assertLogs("core.services", level="WARNING") as logs:
            with self.assertRaisesRegex(AdapterError, "HTTP 403"):
                AppleCanadaAdapter().validate("https://www.apple.com/ca/shop/", "A1A1A1", external_id="MG854VC/A")
        self.assertEqual(logs.output, ["WARNING:core.services:Apple delivery request blocked: HTTP 403; raw response: Access Denied"])
