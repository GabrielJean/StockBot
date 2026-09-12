import base64
import hashlib
import json
import logging
from dataclasses import dataclass
from urllib.parse import urlparse, urlunparse

import requests
from bs4 import BeautifulSoup
from cryptography.fernet import Fernet
from django.conf import settings
from django.utils import timezone


logger = logging.getLogger(__name__)


class AdapterError(Exception):
    pass


@dataclass
class ProductSnapshot:
    canonical_url: str
    title: str
    image_url: str = ""
    price: str = ""
    availability: str = "unknown"
    external_id: str = ""


def _fernet():
    key = settings.WEBHOOK_ENCRYPTION_KEY
    if not key:
        if not settings.DEBUG:
            raise RuntimeError("WEBHOOK_ENCRYPTION_KEY must be configured")
        key = base64.urlsafe_b64encode(b"stockbot-development-key-32-bytes!")
    key_bytes = key.encode() if isinstance(key, str) else key
    try:
        return Fernet(key_bytes)
    except ValueError:
        # Permit a conventional deployment secret while preserving Fernet encryption.
        return Fernet(base64.urlsafe_b64encode(hashlib.sha256(key_bytes).digest()))


def encrypt(value):
    return _fernet().encrypt(value.encode()).decode()


def decrypt(value):
    return _fernet().decrypt(value.encode()).decode()


def masked_url(value):
    parsed = urlparse(value)
    suffix = parsed.path.rsplit("/", 1)[-1]
    return f"{parsed.scheme}://{parsed.netloc}/.../{suffix[-6:]}"


class NintendoCanadaAdapter:
    key = "nintendo_ca"
    domains = {"www.nintendo.com", "nintendo.com"}

    def can_handle_url(self, raw_url):
        parsed = urlparse(raw_url)
        return parsed.scheme in {"http", "https"} and parsed.netloc.lower() in self.domains and "/en-ca/" in parsed.path

    def canonicalize(self, raw_url):
        parsed = urlparse(raw_url)
        return urlunparse(("https", "www.nintendo.com", parsed.path.rstrip("/"), "", "", ""))

    def validate(self, raw_url, postal_code="", fulfillment="shipping", location_keys=None, external_id=""):
        if not self.can_handle_url(raw_url):
            raise AdapterError("Use a Nintendo Canada product URL.")
        return self._fetch(self.canonicalize(raw_url))

    def check(self, product, postal_code="", fulfillment="shipping", location_keys=None):
        return self._fetch(product.canonical_url)

    def _fetch(self, url):
        try:
            response = requests.get(url, timeout=settings.NINTENDO_TIMEOUT_SECONDS, headers={"User-Agent": settings.STOCKBOT_USER_AGENT, "Accept-Language": "en-CA,en;q=0.9"})
        except requests.RequestException as exc:
            raise AdapterError(f"Network error: {exc.__class__.__name__}") from exc
        if response.status_code in {401, 403, 429}:
            raise AdapterError("Retailer access is currently blocked.")
        if response.status_code != 200:
            raise AdapterError(f"Retailer returned HTTP {response.status_code}.")
        soup = BeautifulSoup(response.text, "html.parser")
        title = ""
        image = ""
        price = ""
        availability = "unknown"
        for script in soup.select('script[type="application/ld+json"]'):
            try:
                payload = json.loads(script.string or "{}")
            except json.JSONDecodeError:
                continue
            for value in self._product_nodes(payload):
                title = value.get("name", title)
                image_value = value.get("image", image)
                image = image_value[0] if isinstance(image_value, list) else image_value
                offers = value.get("offers", {})
                if isinstance(offers, list):
                    offers = offers[0] if offers else {}
                if offers.get("price"):
                    currency = offers.get("priceCurrency", "CAD")
                    price = f"${offers['price']} {currency}"
                stock = str(offers.get("availability", "")).rsplit("/", 1)[-1].lower()
                if stock == "instock":
                    availability = "available"
                elif stock in {"outofstock", "soldout", "discontinued"}:
                    availability = "unavailable"
        title = title or (soup.select_one("meta[property='og:title']") or {}).get("content", "")
        image = image or (soup.select_one("meta[property='og:image']") or {}).get("content", "")
        text = soup.get_text(" ", strip=True).lower()
        if availability == "unknown":
            if any(token in text for token in ("out of stock", "sold out", "currently unavailable")):
                availability = "unavailable"
            elif any(token in text for token in ("add to cart", "add to bag", "buy now")):
                availability = "available"
        if not title:
            raise AdapterError("The page did not contain a recognizable product.")
        return ProductSnapshot(url, title[:255], image or "", price, availability)

    def _product_nodes(self, payload):
        """Yield Product dictionaries from plain, list, and @graph JSON-LD documents."""
        if isinstance(payload, list):
            for item in payload:
                yield from self._product_nodes(item)
            return
        if not isinstance(payload, dict):
            return
        types = payload.get("@type", [])
        if isinstance(types, str):
            types = [types]
        if "Product" in types:
            yield payload
        if "@graph" in payload:
            yield from self._product_nodes(payload["@graph"])


class BestBuyCanadaAdapter:
    key = "bestbuy_ca"
    domains = {"www.bestbuy.ca", "bestbuy.ca"}

    def can_handle_url(self, raw_url):
        parsed = urlparse(raw_url)
        return parsed.scheme in {"http", "https"} and parsed.netloc.lower() in self.domains and "/en-ca/product/" in parsed.path and parsed.path.rstrip("/").rsplit("/", 1)[-1].isdigit()

    def canonicalize(self, raw_url):
        parsed = urlparse(raw_url)
        return urlunparse(("https", "www.bestbuy.ca", parsed.path.rstrip("/"), "", "", ""))

    def validate(self, raw_url, postal_code="", fulfillment="shipping", location_keys=None, external_id=""):
        if not self.can_handle_url(raw_url):
            raise AdapterError("Use a Best Buy Canada product URL.")
        # Product confirmation should stay responsive; pickup stock is checked by the monitor.
        return self._fetch(self.canonicalize(raw_url), postal_code, "shipping", [])

    def check(self, product, postal_code="", fulfillment="shipping", location_keys=None):
        return self._fetch(product.canonical_url, postal_code, fulfillment, location_keys or [])

    def nearby_stores(self, postal_code):
        try:
            response = requests.get("https://www.bestbuy.ca/api/v3/json/locations", params={"lang": "en-CA", "postalCode": postal_code}, timeout=settings.NINTENDO_TIMEOUT_SECONDS, headers={"User-Agent": settings.STOCKBOT_USER_AGENT, "Accept": "application/json"})
            response.raise_for_status()
            locations = response.json().get("locations", [])
        except (requests.RequestException, ValueError) as exc:
            raise AdapterError("Best Buy stores are currently unavailable.") from exc
        return [{"id": str(item["locationId"]), "name": item["name"], "city": item.get("city", ""), "region": item.get("region", ""), "distance": item.get("distance"), "pickup": "IN_STORE_PICKUP" in item.get("qpu", {}).get("pickupOptions", [])} for item in locations if item.get("locationId") and "IN_STORE_PICKUP" in item.get("qpu", {}).get("pickupOptions", [])]

    def _fetch(self, url, postal_code="", fulfillment="shipping", location_keys=None):
        sku = url.rsplit("/", 1)[-1]
        headers = {"User-Agent": settings.STOCKBOT_USER_AGENT, "Accept": "application/json", "Accept-Language": "en-CA"}
        try:
            product_response = requests.get(f"https://www.bestbuy.ca/api/v2/json/product/{sku}", timeout=settings.NINTENDO_TIMEOUT_SECONDS, headers=headers)
        except requests.RequestException as exc:
            raise AdapterError(f"Network error: {exc.__class__.__name__}") from exc
        if product_response.status_code in {401, 403, 429}:
            raise AdapterError("Retailer access is currently blocked.")
        if product_response.status_code != 200:
            raise AdapterError("Best Buy product details are currently unavailable.")
        try:
            product = product_response.json()
        except ValueError as exc:
            raise AdapterError("Best Buy returned unexpected product details.") from exc
        product_availability = product.get("availability", {})
        shipping_available = product_availability.get("isAvailableOnline") is True and str(product_availability.get("onlineAvailability", "")).lower() == "instock"
        pickup_available = False
        if fulfillment in {"pickup", "either"}:
            try:
                pickup_response = requests.get("https://www.bestbuy.ca/ecomm-api/availability/products", params={"accept": "application/vnd.bestbuy.standardproduct.v1+json", "accept-language": "en-CA", "locations": "|".join(location_keys or []), "postalCode": postal_code, "skus": sku}, timeout=settings.NINTENDO_TIMEOUT_SECONDS, headers=headers)
                pickup_response.raise_for_status()
                pickup_locations = pickup_response.json()["availabilities"][0]["pickup"]["locations"]
                pickup_available = any(item.get("locationKey") in (location_keys or []) and item.get("hasInventory") and item.get("isReservable") for item in pickup_locations)
            except (requests.RequestException, ValueError, KeyError, IndexError, TypeError) as exc:
                raise AdapterError("Best Buy pickup availability is currently unavailable.") from exc
        in_stock = pickup_available if fulfillment == "pickup" else shipping_available or pickup_available
        image = product.get("highResImage") or product.get("thumbnailImage") or ""
        return ProductSnapshot(url, product.get("name", "")[:255], image, f"${product.get('salePrice', product.get('regularPrice', ''))} CAD", "available" if in_stock else "unavailable")


class AppleCanadaAdapter:
    key = "apple_ca"
    domains = {"www.apple.com", "apple.com"}

    def can_handle_url(self, raw_url):
        parsed = urlparse(raw_url)
        return parsed.scheme in {"http", "https"} and parsed.netloc.lower() in self.domains and parsed.path.startswith("/ca/shop/")

    def validate(self, raw_url, postal_code="", fulfillment="shipping", location_keys=None, external_id=""):
        part = external_id.strip().upper()
        if not self.can_handle_url(raw_url) or not part:
            raise AdapterError("Use an Apple Canada store link and Canada Apple Order No.")
        parsed = urlparse(raw_url)
        url = urlunparse(("https", "www.apple.com", parsed.path.rstrip("/"), "", f"part={part}", ""))
        return self._fetch(url, part, postal_code, fulfillment, location_keys or [])

    def check(self, product, postal_code="", fulfillment="shipping", location_keys=None):
        return self._fetch(product.canonical_url, product.external_id, postal_code, fulfillment, location_keys or [])

    def nearby_stores(self, postal_code, part):
        body = self._pickup_response("https://www.apple.com/ca/shop/", part, postal_code)
        stores = self._stores(body)
        return [
            {
                "id": self._store_id(store),
                "name": self._store_value(store, "name", "storeName") or "Apple Store",
                "city": self._store_address_value(store, "city"),
                "region": self._store_address_value(store, "state") or self._store_value(store, "region"),
                "distance": store.get("storedistance", store.get("distance")),
                "pickup": True,
            }
            for store in stores
            if self._store_id(store)
        ]

    def _fetch(self, url, part, postal_code="", fulfillment="shipping", location_keys=None):
        if fulfillment == "shipping":
            available = self._delivery_available(url, part)
        elif fulfillment == "pickup":
            available = self._pickup_available(url, part, postal_code, location_keys or [])
        else:
            pickup_available = self._pickup_available(url, part, postal_code, location_keys or [])
            try:
                shipping_available = self._delivery_available(url, part)
            except AdapterError:
                if pickup_available:
                    available = True
                else:
                    raise
            else:
                available = shipping_available or pickup_available
        return ProductSnapshot(url, f"Apple product {part}", "", "", "available" if available else "unavailable", part)

    def _pickup_available(self, url, part, postal_code, location_keys):
        body = self._pickup_response(url, part, postal_code)
        stores = self._stores(body)
        selected_stores = [store for store in stores if not location_keys or self._store_id(store) in location_keys]
        parts_availability = [store.get("partsAvailability") for store in selected_stores]
        if not stores:
            parts_availability.append(body.get("partsAvailability"))
        return any(
            isinstance(availability, dict)
            and isinstance(availability.get(part), dict)
            and availability[part].get("pickupDisplay") == "available"
            for availability in parts_availability
        )

    def _delivery_available(self, url, part):
        headers = {
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en-CA",
            "Referer": url,
            "User-Agent": settings.STOCKBOT_USER_AGENT,
        }
        params = {
            "mt": "regular",
            "parts.0": part,
        }
        try:
            response = requests.get("https://www.apple.com/ca/shop/delivery-message", params=params, timeout=settings.NINTENDO_TIMEOUT_SECONDS, headers=headers)
            if response.status_code in {403, 429, 541}:
                detail = response.text[:10000]
                logger.warning("Apple delivery request blocked: HTTP %s; raw response: %s", response.status_code, detail or "<empty>")
                raise AdapterError(f"Apple blocked this automated delivery request (HTTP {response.status_code}).")
            response.raise_for_status()
            messages = response.json()["body"]["content"]["deliveryMessage"]
            message_sets = next(value for key, value in messages.items() if part in key.upper() and isinstance(value, dict))
            message = message_sets["regular"]
            delivery_options = message.get("deliveryOptions", [])
            if not isinstance(delivery_options, list):
                raise TypeError("deliveryOptions")
        except AdapterError:
            raise
        except (requests.RequestException, ValueError, KeyError, StopIteration, TypeError) as exc:
            raise AdapterError("Apple delivery availability is currently unavailable.") from exc
        return any(isinstance(option, dict) for option in delivery_options)

    def _pickup_response(self, url, part, postal_code):
        if not postal_code:
            raise AdapterError("Apple fulfillment requires a Canadian postal code.")
        headers = {
            "Accept": "*/*",
            "Accept-Language": "en-CA",
            "Referer": url,
            "User-Agent": settings.STOCKBOT_USER_AGENT,
        }
        params = {
            "pl": "true",
            "parts.0": part,
            "location": postal_code,
        }
        try:
            response = requests.get("https://www.apple.com/ca/shop/retail/pickup-message", params=params, timeout=settings.NINTENDO_TIMEOUT_SECONDS, headers=headers)
            if response.status_code in {403, 429, 541}:
                detail = response.text[:10000]
                logger.warning("Apple fulfillment request blocked: HTTP %s; raw response: %s", response.status_code, detail or "<empty>")
                raise AdapterError(f"Apple blocked this automated fulfillment request (HTTP {response.status_code}).")
            response.raise_for_status()
            body = response.json()["body"]
            if not isinstance(body, dict):
                raise TypeError("body")
            return body
        except AdapterError:
            raise
        except (requests.RequestException, ValueError, KeyError, StopIteration, TypeError) as exc:
            raise AdapterError("Apple fulfillment is currently unavailable.") from exc

    def _stores(self, body):
        stores = body.get("stores")
        if stores is None:
            stores = body.get("content", {}).get("pickupMessage", {}).get("stores")
        if not isinstance(stores, list):
            return []
        return [store for store in stores if isinstance(store, dict)]

    def _store_id(self, store):
        return str(self._store_value(store, "storeNumber", "storeId", "id") or "")

    def _store_value(self, store, *keys):
        retail_store = store.get("retailStore")
        if not isinstance(retail_store, dict):
            retail_store = {}
        for key in keys:
            value = store.get(key) or retail_store.get(key)
            if value:
                return value
        return ""

    def _store_address_value(self, store, key):
        retail_store = store.get("retailStore")
        if not isinstance(retail_store, dict):
            retail_store = {}
        address = retail_store.get("address")
        if not isinstance(address, dict):
            address = store.get("address") if isinstance(store.get("address"), dict) else {}
        return address.get(key) or store.get(key) or ""


adapters = [NintendoCanadaAdapter(), BestBuyCanadaAdapter(), AppleCanadaAdapter()]


def get_adapter_for_url(raw_url):
    return next((item for item in adapters if item.can_handle_url(raw_url)), None)


def get_adapter_for_retailer(retailer):
    return next((item for item in adapters if item.key == retailer), None)


def post_discord(webhook, title, url, price="", test=False):
    target = decrypt(webhook.encrypted_url)
    description = "This is a StockBot delivery test." if test else "StockBot reports this item is available."
    payload = {"embeds": [{"title": title, "url": url, "description": description, "color": 3066993, "fields": ([{"name": "Price", "value": price, "inline": True}] if price else [])}]}
    try:
        result = requests.post(target, json=payload, timeout=10)
        result.raise_for_status()
    except requests.RequestException as exc:
        webhook.last_delivery_error = exc.__class__.__name__
        webhook.save(update_fields=["last_delivery_error"])
        return False, webhook.last_delivery_error
    webhook.last_delivery_error = ""
    webhook.last_tested_at = timezone.now() if test else webhook.last_tested_at
    webhook.save(update_fields=["last_delivery_error", "last_tested_at"])
    return True, ""
