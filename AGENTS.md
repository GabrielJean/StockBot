# StockBot Contributor Instructions

## Project Overview

StockBot is a self-hosted Canadian retail-stock monitor. It uses a Django backend, a React/Vite single-page dashboard, SQLite persistence, an embedded APScheduler, retailer-specific availability adapters, and Discord webhook notifications.

Key directories:

- `core/`: Django models, JSON API views, retailer adapters, monitoring logic, migrations, and tests.
- `config/`: Django configuration and URL routing.
- `frontend/`: React source built by Vite into `core/static/`.
- `templates/index.html`: Django entry page for the SPA.
- `docker-entrypoint.sh`, `Dockerfile`, `docker-compose.yml`: production runtime and deployment configuration.

## Development Commands

Use Docker for all application builds, runtime debugging, and verification. The Docker image defines the supported Python 3.13 and Node 22 environment; do not rely on host-installed language runtimes for project work.

Build and run the production-equivalent service locally with Docker:

```sh
docker compose up --build
```

Use the running container for diagnostics and targeted checks:

```sh
docker compose exec app python manage.py test
docker compose exec app python manage.py migrate
docker compose logs --follow app
```

The Docker build runs `npm ci` and builds the frontend. Rebuild the image after changing `frontend/`, Python dependencies, Docker configuration, or runtime scripts.

Do not commit generated `core/static/`, `frontend/node_modules/`, SQLite databases, or runtime secrets.

## Backend Conventions

- Keep the backend dependency-light. Add a dependency only when the existing Django, standard-library, `requests`, BeautifulSoup, cryptography, or APScheduler tools cannot solve the problem cleanly.
- The API is implemented as JSON responses in `core/views.py`, not Django REST Framework. Keep payload fields camelCase to match the frontend and existing API responses.
- Authentication-changing requests require Django CSRF protection. The frontend obtains a token from `/api/v1/` and sends `X-CSRFToken` for non-GET requests. Preserve this behavior for every new mutation.
- Scope user-owned resources through `request.user` unless the endpoint explicitly requires staff access. Check approval status via `require_user()` before exposing or mutating account data.
- Create a Django migration for every model change. Do not alter applied migration files.
- Use `transaction.atomic()` and row locking where state transitions can race, particularly account creation, monitor arming, and notification delivery.
- Add or update `core/tests.py` tests for API, authorization, model, monitoring, and adapter behavior changes. Mock retailer and Discord HTTP calls; tests must not make external requests.

## Monitoring And Notifications

- `Product` represents retailer-wide product information; `Monitor` stores user-specific destination, fulfillment, and check-interval settings. Keep this distinction intact.
- Availability may be `available`, `unavailable`, `unknown`, `error`, or `blocked`. Never convert retailer parsing failures, access denial, CAPTCHA-like responses, or network errors into `unavailable`.
- Alerts fire only on an unavailable-to-available transition for an armed monitor. A confirmed unavailable state rearms it. Preserve this invariant and deliver Discord notifications only after database transactions complete.
- The production process intentionally runs one Gunicorn worker and one embedded scheduler against SQLite. Do not add workers, replicas, or a second scheduler process without redesigning scheduling and database coordination.
- Retain the bounded retry behavior and 30-day `CheckResult` cleanup in `check_due_products()` when modifying monitoring cadence.

## Retailer Adapter Rules

- Implement retailer behavior in a dedicated adapter in `core/services.py` with `can_handle_url`, `validate`, and `check` methods. Add `nearby_stores` only for pickup-capable retailers.
- Canonicalize product URLs before persistence, use the configured `STOCKBOT_USER_AGENT`, set explicit request timeouts, and turn expected HTTP/API failures into `AdapterError` messages suitable for users.
- Do not bypass retailer protections, imitate browser sessions, evade rate limits, solve CAPTCHAs, or use undocumented access-control workarounds. Surface blocked responses as `blocked` or an adapter error.
- Add mocked fixtures for success, unavailable, malformed payload, blocked, and network-error paths when changing adapter logic.

## Service Layer Details

`core/services.py` is the boundary between the application and external retailers or Discord. Keep request construction, parsing, canonicalization, and external error translation in this module; views and monitoring code should consume adapter methods rather than issue HTTP calls directly.

### Adapter Contract

- `can_handle_url(raw_url)` must be a cheap, side-effect-free URL eligibility check. It must only accept the retailer's intended Canadian URL shape and HTTPS/HTTP URLs.
- `canonicalize(raw_url)` must remove tracking data and normalize the persisted identity. `Product.canonical_url` is unique, so changing canonicalization changes de-duplication behavior and requires migration planning for existing data.
- `validate(raw_url, postal_code="", fulfillment="shipping", location_keys=None, external_id="")` is called before monitor creation. It must validate retailer-specific inputs and return a current `ProductSnapshot` without writing database records.
- `check(product, postal_code="", fulfillment="shipping", location_keys=None)` is called by the scheduler. It must return `ProductSnapshot` for the exact product and monitor fulfillment settings.
- `ProductSnapshot` fields are `canonical_url`, `title`, `image_url`, `price`, `availability`, and optional `external_id`. Keep title values suitable for the `Product.title` length limit and ensure a known availability value is returned.
- Raise `AdapterError` for expected request, response, and parsing failures. Keep messages safe to show in the UI and do not include secrets, full webhook URLs, cookies, or large raw retailer responses.
- Set `timeout=settings.NINTENDO_TIMEOUT_SECONDS` and send the transparent `settings.STOCKBOT_USER_AGENT` for every retailer request. Add an appropriate `Accept` and `Accept-Language: en-CA` header when the retailer expects them.
- Treat HTTP `401`, `403`, and `429`, CAPTCHA-like payloads, or explicit retailer access denials as blocked. Do not silently retry in the adapter and do not interpret access failure as stock availability.

### Existing Retailer Behavior

- `NintendoCanadaAdapter` parses page JSON-LD only for product metadata. Stock status comes from Nintendo's documented saleability response when a SKU can be derived from the canonical URL. Missing or malformed saleability data must remain `unknown`, not use stale or ambiguous page stock labels.
- `BestBuyCanadaAdapter` checks product data for shipping availability. Pickup availability is monitor-specific: it requires a Canadian postal code and selected Best Buy location keys, and it is evaluated only for `pickup` or `either` fulfillment.
- `AppleCanadaAdapter` identifies the product by a Canada Apple Order No. stored in `Product.external_id`. Shipping uses the delivery-message endpoint; pickup uses the pickup-message endpoint with a postal code and optionally selected store IDs. For `either`, a successful pickup result may establish availability when shipping is blocked, but a blocked or malformed response must not be treated as unavailable.
- `nearby_stores()` returns only selectable pickup locations with stable string IDs and the UI fields `id`, `name`, `city`, `region`, `distance`, and `pickup`. Do not persist unselected search results.
- Register a completed adapter in `adapters`, and ensure `get_adapter_for_url()` and `get_adapter_for_retailer()` can find it. Also add the appropriate enabled store metadata and validation route behavior in `core/views.py`, then expose it in the frontend.

### Encryption And Discord Delivery

- Persist webhook URLs only as `DiscordWebhook.encrypted_url`, using `encrypt()` before saving. Use `decrypt()` only immediately before a Discord request.
- API serializers must return `masked_url(decrypt(...))`, never the full URL, encrypted value, or derived Discord token.
- `post_discord()` owns Discord payload construction and request execution. It returns `(delivered, error)` and updates webhook delivery metadata; callers must create `NotificationDelivery` audit records after the attempt.
- A failed Discord request is a delivery failure, not a monitor or retailer availability failure. Retain the error class only, rather than response content or sensitive endpoint data.

### Monitoring Flow

- `check_due_products()` records scheduler start/completion timestamps, processes at most 100 due products in oldest-due order, and deletes `CheckResult` records older than 30 days. Preserve all four responsibilities.
- `/healthz/` is a scheduler-readiness check. With scheduling enabled, it must return a non-2xx response when the last completed pass is absent or older than `SCHEDULER_STALE_SECONDS`; Docker uses it to restart a service with stalled checks.
- Nintendo-like shared availability is checked once per `Product`. Best Buy and Apple availability depends on monitor fulfillment/store selections, so `check_product()` calls `check_monitor()` once per active monitor.
- Check intervals belong to `Monitor`, not `Product`. The scheduler runs every 30 seconds to support the fastest allowed interval, but only monitors whose `next_check_at` is due may be updated, alerted, or rescheduled.
- Successful checks update product metadata, `last_checked_at`, `last_success_at`, availability, and the next regular check. Failed checks set `error` or `blocked`, retain prior product metadata, and use the bounded slower retry interval.
- Lock the affected `Product` or `Monitor` row while reading prior availability, updating `armed`, and recording state. This prevents simultaneous checks from sending duplicate restock alerts.
- An `unavailable` result always rearms the monitor. An `available` result disarms and alerts only when the preceding result was not available and the monitor was armed. `unknown`, `error`, and `blocked` neither rearm nor generate alerts.
- Collect deliveries while inside the database transaction, but call `post_discord()` only after it commits. This avoids notifying users about state that may roll back.

### API And UI Integration

- `core/views.py` is a resource-dispatch JSON API, not a REST framework application. Keep existing endpoint paths and trailing-slash behavior; the React `request()` helper normalizes paths and supplies credentials and CSRF headers.
- The monitor creation sequence is `validate` then `confirm-monitor`. `Validation` is user-owned, expires after 15 minutes, stores the requested fulfillment configuration, and must be consumed only by its owner.
- Preserve ownership checks for monitors and webhooks. Staff endpoints may access all resources only after an explicit `request.user.is_staff` check.
- When adding serialized data, update the relevant serializer, frontend consumers in `frontend/src/main.jsx`, and API tests in the same change. Dates are returned as Django JSON values and rendered by the frontend's formatting helpers.

## Secrets And Deployment

- Never expose, log, serialize, or commit Discord webhook URLs, `.env` values, `DJANGO_SECRET_KEY`, `WEBHOOK_ENCRYPTION_KEY`, CI secrets, or Portainer credentials.
- Webhook URLs are encrypted at rest. Continue using `encrypt()`/`decrypt()` and return only `masked_url()` values to clients.
- `docker-entrypoint.sh` creates durable runtime secrets in `/data/stockbot.env` when they are absent. Do not replace these on container restart; replacing `WEBHOOK_ENCRYPTION_KEY` makes stored webhooks unreadable.
- Production configuration comes from environment variables. Keep `ALLOWED_HOSTS`, `CSRF_TRUSTED_ORIGINS`, and secure-cookie settings deployment-configurable; do not hardcode private deployment domains in application logic.
- The GitHub workflow builds and deploys on pushes to `main`. Treat changes to Docker, Compose, or workflow files as production-impacting and validate the image build before merging.

## Frontend Conventions

- Keep UI work in `frontend/src/`; Vite emits the production bundle into `core/static/`.
- The frontend is intentionally plain React with browser `fetch`, component-local state, and CSS files. Follow this approach unless a broader refactor is explicitly requested.
- Use the existing `request()` helper for API calls so credentials, JSON handling, trailing slashes, and CSRF headers remain consistent.
- Update the backend serialization, frontend rendering, and tests together when changing API fields or monitor state.
- Keep the dashboard responsive and accessible: use semantic controls, labels, visible error feedback, and keyboard-operable interactions.

## Verification Expectations

Before completing a change, run the smallest relevant checks and report what ran:

- Backend changes: `docker compose exec app python manage.py test`.
- Model changes: generate and inspect migrations in the Docker container, then run tests there.
- Frontend changes: `docker compose build` to run the Vite build in the supported Node image.
- Docker/runtime changes: `docker compose build`, then start the service and inspect its health endpoint and logs.

Do not modify unrelated code, generated artifacts, or environment files. Preserve any uncommitted changes made by others.
