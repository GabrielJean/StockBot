from unittest.mock import Mock, patch
from datetime import timedelta

from django.test import Client, TestCase, override_settings
from django.db import OperationalError, connection
from django.utils import timezone

from .models import DiscordWebhook, Monitor, NotificationDelivery, Product, SystemState, User, Validation
from .monitoring import check_due_products, check_monitor
from .services import AppleCanadaAdapter, AdapterError, BestBuyCanadaAdapter, NintendoCanadaAdapter, ProductSnapshot, decrypt, encrypt


@override_settings(WEBHOOK_ENCRYPTION_KEY="HsdQznbn2wz1LikNoUvwzmgskkODlG5pgvAd1uKVXpQ=")
class AccountAndMonitorTests(TestCase):
    def test_health_requires_a_recent_completed_scheduler_pass(self):
        response = self.client.get("/healthz/")
        self.assertEqual(response.status_code, 503)
        self.assertFalse(response.json()["schedulerHealthy"])

        now = timezone.now()
        SystemState.objects.create(pk=1, scheduler_heartbeat=now, scheduler_completed_at=now)
        response = self.client.get("/healthz/")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["schedulerHealthy"])

    @override_settings(SCHEDULER_STALE_SECONDS=60)
    def test_health_rejects_a_stale_completed_scheduler_pass(self):
        stale = timezone.now() - timedelta(seconds=61)
        SystemState.objects.create(pk=1, scheduler_heartbeat=stale, scheduler_completed_at=stale)

        response = self.client.get("/healthz/")

        self.assertEqual(response.status_code, 503)
        self.assertFalse(response.json()["ok"])

    @override_settings(SCHEDULER_ENABLED=False)
    def test_health_allows_an_intentionally_disabled_scheduler(self):
        response = self.client.get("/healthz/")

        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["schedulerEnabled"])

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

    def test_staff_can_manage_any_monitor(self):
        admin = User.objects.create_user(email="admin@example.com", password="A-secure-passphrase-123", status=User.Status.APPROVED, is_active=True, is_staff=True)
        owner = User.objects.create_user(email="owner@example.com", password="A-secure-passphrase-123", status=User.Status.APPROVED, is_active=True)
        product = Product.objects.create(canonical_url="https://www.nintendo.com/en-ca/store/products/example", title="Example")
        webhook = DiscordWebhook.objects.create(owner=owner, name="Discord", encrypted_url=encrypt("https://discord.com/api/webhooks/1/token"))
        monitor = Monitor.objects.create(owner=owner, product=product, webhook=webhook)

        self.client.force_login(owner)
        self.assertEqual(self.client.get("/api/v1/staff-monitors/").status_code, 403)

        self.client.force_login(admin)
        response = self.client.get("/api/v1/staff-monitors/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["monitors"][0]["owner"]["email"], owner.email)
        self.assertEqual(self.client.patch(f"/api/v1/staff-monitors/{monitor.id}/", data={"active": False}, content_type="application/json").status_code, 200)
        monitor.refresh_from_db()
        self.assertFalse(monitor.active)
        self.assertEqual(self.client.delete(f"/api/v1/staff-monitors/{monitor.id}/").status_code, 200)
        self.assertFalse(Monitor.objects.filter(id=monitor.id).exists())

    def test_available_check_records_monitor_availability_time(self):
        owner = User.objects.create_user(email="owner@example.com", password="A-secure-passphrase-123", status=User.Status.APPROVED, is_active=True)
        product = Product.objects.create(canonical_url="https://www.nintendo.com/en-ca/store/products/example", title="Example")
        webhook = DiscordWebhook.objects.create(owner=owner, name="Discord", encrypted_url=encrypt("https://discord.com/api/webhooks/1/token"))
        monitor = Monitor.objects.create(owner=owner, product=product, webhook=webhook, armed=False)
        checked_at = timezone.now()
        source = Mock()
        source.check.return_value = ProductSnapshot(product.canonical_url, product.title, availability=Product.Availability.AVAILABLE)

        check_monitor(monitor, product, source, checked_at)

        monitor.refresh_from_db()
        self.assertEqual(monitor.last_available_at, checked_at)
        self.assertEqual(monitor.next_check_at, checked_at + timedelta(seconds=60))

    def test_monitor_interval_can_be_updated_by_its_owner(self):
        owner = User.objects.create_user(email="owner@example.com", password="A-secure-passphrase-123", status=User.Status.APPROVED, is_active=True)
        product = Product.objects.create(canonical_url="https://www.nintendo.com/en-ca/store/products/example", title="Example")
        webhook = DiscordWebhook.objects.create(owner=owner, name="Discord", encrypted_url=encrypt("https://discord.com/api/webhooks/1/token"))
        monitor = Monitor.objects.create(owner=owner, product=product, webhook=webhook, next_check_at=timezone.now() + timedelta(hours=1))

        self.client.force_login(owner)
        response = self.client.patch(f"/api/v1/monitors/{monitor.id}/", data={"checkIntervalSeconds": 900}, content_type="application/json")

        monitor.refresh_from_db()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["monitor"]["checkIntervalSeconds"], 900)
        self.assertEqual(monitor.check_interval_seconds, 900)
        self.assertIsNone(monitor.next_check_at)

    def test_monitor_interval_rejects_unsupported_values(self):
        owner = User.objects.create_user(email="owner@example.com", password="A-secure-passphrase-123", status=User.Status.APPROVED, is_active=True)
        product = Product.objects.create(canonical_url="https://www.nintendo.com/en-ca/store/products/example", title="Example")
        webhook = DiscordWebhook.objects.create(owner=owner, name="Discord", encrypted_url=encrypt("https://discord.com/api/webhooks/1/token"))
        monitor = Monitor.objects.create(owner=owner, product=product, webhook=webhook)

        self.client.force_login(owner)
        response = self.client.patch(f"/api/v1/monitors/{monitor.id}/", data={"checkIntervalSeconds": 120}, content_type="application/json")

        self.assertEqual(response.status_code, 400)
        monitor.refresh_from_db()
        self.assertEqual(monitor.check_interval_seconds, 60)

    def test_monitor_delivery_runs_after_transaction_commits(self):
        owner = User.objects.create_user(email="owner@example.com", password="A-secure-passphrase-123", status=User.Status.APPROVED, is_active=True)
        product = Product.objects.create(canonical_url="https://www.nintendo.com/en-ca/store/products/example", title="Example")
        webhook = DiscordWebhook.objects.create(owner=owner, name="Discord", encrypted_url=encrypt("https://discord.com/api/webhooks/1/token"))
        monitor = Monitor.objects.create(owner=owner, product=product, webhook=webhook)
        source = Mock()
        source.check.return_value = ProductSnapshot(product.canonical_url, product.title, availability=Product.Availability.AVAILABLE)
        transaction_state = []
        outer_transaction_state = tuple(connection.savepoint_ids)

        with patch("core.monitoring.post_discord", side_effect=lambda *_: (transaction_state.append(tuple(connection.savepoint_ids)), (True, ""))[1]):
            check_monitor(monitor, product, source, timezone.now())

        self.assertEqual(transaction_state, [outer_transaction_state])
        self.assertEqual(NotificationDelivery.objects.filter(monitor=monitor).count(), 1)

    def test_unexpected_monitor_check_failure_records_an_error(self):
        owner = User.objects.create_user(email="owner@example.com", password="A-secure-passphrase-123", status=User.Status.APPROVED, is_active=True)
        product = Product.objects.create(canonical_url="https://www.nintendo.com/en-ca/store/products/example", title="Example")
        webhook = DiscordWebhook.objects.create(owner=owner, name="Discord", encrypted_url=encrypt("https://discord.com/api/webhooks/1/token"))
        monitor = Monitor.objects.create(owner=owner, product=product, webhook=webhook)
        source = Mock()
        source.check.side_effect = ValueError("Unexpected response shape")

        with self.assertLogs("core.monitoring", level="ERROR"):
            check_monitor(monitor, product, source, timezone.now())

        monitor.refresh_from_db()
        self.assertEqual(monitor.availability, Product.Availability.ERROR)
        self.assertEqual(monitor.last_error, "Retailer availability is currently unavailable.")
        self.assertIsNotNone(monitor.last_checked_at)

    def test_due_product_failure_does_not_stop_later_checks(self):
        owner = User.objects.create_user(email="owner@example.com", password="A-secure-passphrase-123", status=User.Status.APPROVED, is_active=True)
        webhook = DiscordWebhook.objects.create(owner=owner, name="Discord", encrypted_url=encrypt("https://discord.com/api/webhooks/1/token"))
        first = Product.objects.create(canonical_url="https://www.nintendo.com/en-ca/store/products/first", title="First", next_check_at=timezone.now() - timedelta(minutes=1))
        second = Product.objects.create(canonical_url="https://www.nintendo.com/en-ca/store/products/second", title="Second", next_check_at=timezone.now() - timedelta(minutes=1))
        Monitor.objects.create(owner=owner, product=first, webhook=webhook)
        Monitor.objects.create(owner=owner, product=second, webhook=webhook)

        with patch("core.monitoring.check_product", side_effect=[RuntimeError("bad product"), None]) as check_product, self.assertLogs("core.monitoring", level="ERROR"):
            check_due_products()

        self.assertEqual(check_product.call_args_list[0].args, (first.id,))
        self.assertEqual(check_product.call_args_list[1].args, (second.id,))
        state = SystemState.objects.get(pk=1)
        self.assertIsNotNone(state.scheduler_started_at)
        self.assertIsNotNone(state.scheduler_completed_at)

    def test_scheduler_retries_a_locked_heartbeat_update(self):
        original = SystemState.objects.update_or_create
        attempts = 0

        def update_or_create(*args, **kwargs):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise OperationalError("database is locked")
            return original(*args, **kwargs)

        with patch.object(SystemState.objects, "update_or_create", side_effect=update_or_create), patch("core.monitoring.time.sleep") as sleep:
            check_due_products()

        self.assertEqual(sleep.call_count, 1)
        self.assertIsNotNone(SystemState.objects.get(pk=1).scheduler_completed_at)

    def test_confirm_apple_monitor_serializes_last_available_time(self):
        owner = User.objects.create_user(email="owner@example.com", password="A-secure-passphrase-123", status=User.Status.APPROVED, is_active=True)
        webhook = DiscordWebhook.objects.create(owner=owner, name="Discord", encrypted_url=encrypt("https://discord.com/api/webhooks/1/token"))
        validation = Validation.objects.create(
            owner=owner,
            canonical_url="https://www.apple.com/ca/shop?part=MJR84VC/A",
            external_id="MJR84VC/A",
            title="Apple product MJR84VC/A",
            availability=Product.Availability.UNAVAILABLE,
            check_interval_seconds=300,
            expires_at=timezone.now() + timedelta(minutes=15),
        )

        self.client.force_login(owner)
        response = self.client.post(f"/api/v1/confirm-monitor/", data={"validationId": validation.id, "webhookId": webhook.id}, content_type="application/json")

        self.assertEqual(response.status_code, 201)
        self.assertIsNone(response.json()["monitor"]["lastAvailableAt"])
        self.assertEqual(response.json()["monitor"]["checkIntervalSeconds"], 300)


class NintendoAdapterTests(TestCase):
    product_url = "https://www.nintendo.com/en-ca/store/products/nintendo-switch-2-camera-123682/"

    def fetch_snapshot(self, availability, saleable_quantity=None):
        html = f'''<script type="application/ld+json">{{"@context":"https://schema.org/","@graph":[{{"@type":["Product"],"name":"Nintendo Switch 2 Example","image":"https://images.example/product.jpg","offers":{{"price":"69.99","priceCurrency":"CAD","availability":"https://schema.org/{availability}"}}}}]}}</script>'''
        page_response = Mock(status_code=200, text=html)
        graph_response = Mock(status_code=200, json=lambda: {"data": {"product": {"isSalableQty": saleable_quantity}}})
        with patch("core.services.requests.get", side_effect=[graph_response, page_response]):
            return NintendoCanadaAdapter().validate(self.product_url)

    def test_saleable_quantity_marks_product_available(self):
        snapshot = self.fetch_snapshot("OutOfStock", True)
        self.assertEqual(snapshot.availability, "available")
        self.assertEqual(snapshot.price, "$69.99 CAD")

    def test_unsaleable_quantity_marks_product_unavailable(self):
        snapshot = self.fetch_snapshot("InStock", False)
        self.assertEqual(snapshot.availability, "unavailable")

    def test_page_stock_metadata_is_ignored_without_saleable_quantity(self):
        snapshot = self.fetch_snapshot("InStock")
        self.assertEqual(snapshot.availability, "unknown")

    @override_settings(STOCKBOT_USER_AGENT="StockBot/1.0 (contact: ops@example.com)")
    def test_saleability_request_uses_nintendo_graph_headers(self):
        html = '<script type="application/ld+json">{"@type":"Product","name":"Nintendo Switch 2 Example"}</script>'
        page_response = Mock(status_code=200, text=html)
        graph_response = Mock(status_code=200, json=lambda: {"data": {"product": {"isSalableQty": True}}})

        with patch("core.services.requests.get", side_effect=[graph_response, page_response]) as request_get:
            NintendoCanadaAdapter().validate(self.product_url)

        headers = request_get.call_args_list[0].kwargs["headers"]
        self.assertEqual(headers["User-Agent"], "StockBot/1.0 (contact: ops@example.com)")
        self.assertEqual(headers["apollographql-client-name"], "ncom")
        self.assertEqual(headers["locale"], "en-CA")
        self.assertEqual(headers["Origin"], "https://www.nintendo.com")
        self.assertEqual(headers["x-nintendo-graph"], "true")

    def test_graphql_metadata_is_preferred_without_a_page_request(self):
        graph_response = Mock(
            status_code=200,
            json=lambda: {
                "data": {
                    "product": {
                        "sku": "123682",
                        "name": "Nintendo Switch 2 Camera",
                        "prices": {"finalPrice": "69.99", "currency": "CAD"},
                        "isSalableQty": True,
                    }
                }
            },
        )

        with patch("core.services.requests.get", return_value=graph_response) as request_get:
            snapshot = NintendoCanadaAdapter().validate(self.product_url)

        self.assertEqual(request_get.call_count, 1)
        self.assertEqual(snapshot.title, "Nintendo Switch 2 Camera")
        self.assertEqual(snapshot.price, "$69.99 CAD")
        self.assertEqual(snapshot.availability, "available")
        self.assertEqual(snapshot.external_id, "123682")


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
