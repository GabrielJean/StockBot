# Nintendo Canada Stock Monitor

## Scope and Decisions

- Build a public, multi-account stock-monitoring web application for Canada.
- Use Django + Django REST Framework for the API and application domain, with a React + TypeScript SPA for the customer and staff dashboards. Keep Django admin available only as an internal fallback, not as the primary operational UI.
- Use SQLite on a Docker volume. Run one container and one Django/Gunicorn worker process; its in-process scheduler performs the checks. This deployment must not be horizontally replicated while using SQLite and the embedded scheduler.
- Launch one production retailer adapter: Nintendo Canada (`nintendo.com/en-ca`). Display additional retailer cards as coming soon, but do not claim they are supported or accept their URLs.
- Only detect online shipping availability in this release. Pickup monitoring is out of scope until a postal-code/store-selection model is decided.
- Check active products at a fixed 60-second cadence, with per-domain concurrency/rate controls and jitter. There is intentionally no per-user monitor limit.
- Public signup uses email/password. The first successfully registered account becomes the active superuser; every later account is pending and cannot authenticate or use application resources until a staff administrator approves it in the custom admin dashboard. Do not add email verification, password reset email, CAPTCHA, or web-facing authentication throttling in this release, per the selected product policy.
- Each user owns named Discord webhook destinations. A monitor selects one destination. Discord alerts happen only on a confirmed unavailable-to-available transition; the monitor re-arms after a confirmed unavailable result.

## Architecture

1. Create a Django project with domain apps for `accounts`, `retailers`, `monitors`, `notifications`, and `dashboard_api`.
2. Create a Vite React/TypeScript application in `frontend/`; compile it during the Docker build and serve its versioned static bundle from Django. Configure the SPA fallback so direct navigation to dashboard routes works.
3. Use same-origin session-cookie authentication with CSRF protection. The React client calls versioned DRF endpoints; it never receives another user's monitor, webhook, product, or check data.
4. Use Django migrations and SQLite foreign-key constraints. Configure `DATABASE_PATH` to a mounted `/data/db.sqlite3` file; use a database busy timeout and keep scheduler writes short to handle SQLite locking safely.
5. Use a custom `run_service` management command as the image entrypoint. It applies pending migrations, starts the one-process scheduler, and launches Gunicorn with exactly one worker. Add a startup guard so development autoreload/test processes do not start duplicate schedulers.
6. Use APScheduler (or an equivalently small in-process scheduler) for a single periodic dispatcher. It finds distinct products with active monitors, records a scheduler heartbeat, and submits due checks with randomized seconds-level jitter. Enforce one Nintendo check at a time and skip/defer overlapping product checks rather than queueing an unlimited backlog.
7. Build a retailer adapter contract with `can_handle_url(url)`, `validate(url)`, and `check(product)` returning normalized product metadata and a structured availability result. The Nintendo adapter owns URL canonicalization, product identifier extraction, page retrieval, and stock interpretation; controller and scheduler code must never parse retailer HTML.
8. Use Playwright Chromium in the Nintendo adapter when a rendered product page is required, with conservative navigation timeouts and no logged-in retailer session. Do not implement bot-protection bypasses. Treat CAPTCHA, access denial, unexpected markup, and timeouts as a check error, never as an out-of-stock response.
9. Persist a redacted diagnostic code/message and timestamps for errors. Apply bounded exponential retry delays for failed product checks while preserving the normal cadence after a successful check. Expose the error state in the UI and logs without exposing cookies, webhook secrets, or raw page content.

## Data Model

1. Define a custom `User` model before the initial migration: unique email, display name, password hash, account status (`pending`, `approved`, `rejected`), active/staff/superuser flags, joined/approved/last-login timestamps. Registration uses email as the login identity and requires an acceptable password; approved users can edit their display name and password from account settings.
2. Add a singleton `SystemState` row in the initial migration with an `initial_admin_claimed` flag. In one database transaction, the registration endpoint locks this row: the first successful registration claims it and creates an active staff superuser; all subsequent registrations create inactive, pending accounts. This prevents concurrent first signups from creating multiple administrators.
3. Store `DiscordWebhook` records with owner, display name, encrypted webhook URL, enabled state, creation/update timestamps, and last-test/last-delivery results. Encrypt the URL at rest with an application key supplied through `WEBHOOK_ENCRYPTION_KEY`; API responses return only a safe masked endpoint representation.
4. Store a retailer `Product` once per retailer/canonical URL, with retailer key, canonical URL, external identifier when available, title, image URL, displayed price/currency, most recent normalized availability, metadata timestamps, and last successful/error check details.
5. Store an expiring owner-scoped `Validation` record after URL validation. It holds the fetched product snapshot and canonical URL so the confirmation endpoint can create a monitor only from a recently verified, supported Nintendo product.
6. Store `Monitor` subscriptions with owner, product, selected webhook, active/paused status, created/updated timestamps, and per-subscription alert state. Product checks are shared across users monitoring the same canonical Nintendo product; a shared availability transition is evaluated independently for each active monitor.
7. Store compact `CheckResult` history with product, checked time, status (`available`, `unavailable`, `unknown`, `error`, `blocked`), source/fulfillment mode (`shipping`), optional normalized price, and safe error code. Retain a bounded recent history through a scheduled cleanup job so an unlimited monitor set cannot grow SQLite indefinitely.
8. Store `NotificationDelivery` audit records with monitor, webhook, event type, attempted/delivered timestamps, Discord response class, and safe error detail. A non-2xx Discord result disables neither the monitor nor the webhook automatically; show it as actionable configuration health.

## API and Monitoring Flow

1. Implement public endpoints for registration, login, logout, current session, and profile/password update. Require CSRF for session-mutating requests and return consistent field-level validation errors for the SPA. Registration returns whether the account is immediately active (first administrator) or pending; login rejects pending/rejected accounts without issuing a session.
2. Implement user-scoped webhook endpoints to list, create, rename, enable/disable, delete, and explicitly send a labeled test message. Validate Discord webhook URL host/path structure before saving, encrypt on write, and never return the raw value.
3. Expose a supported-store endpoint. Nintendo Canada is enabled; Walmart, Best Buy, Amazon, and future cards are returned as unavailable/coming-soon metadata so the UI can present the planned catalogue without accepting unsupported links.
4. Implement `POST /api/v1/products/validate` to accept a URL, reject unsupported/malformed/non-product Nintendo URLs, invoke the adapter, create the short-lived validation record, and return title, retailer, canonical URL, image, displayed price, and current shipping availability.
5. Implement monitor creation as a confirmation endpoint accepting validation ID and webhook ID. It revalidates ownership/expiry, creates or reuses the canonical product, creates the subscription, and returns its initial state. Do not alert immediately for a product already available; the current availability is shown to the user and future out-of-stock-to-in-stock transitions trigger alerts.
6. Implement monitor list/detail, pause/resume, webhook reassignment, and deletion endpoints. Return current product state, last checked time, last successful check, safe error/blocked state, and a small recent history for the detail view.
7. On each successful product check, update the product snapshot and write a `CheckResult`. When a confirmed unavailable state follows availability, re-arm all affected active monitors. When confirmed availability follows unavailable, enqueue one Discord embed delivery per armed active monitor and atomically mark it alerted so concurrent scheduler runs cannot duplicate alerts.
8. Construct Discord messages with product title, retailer, price when present, shipping availability, canonical product link, image thumbnail when available, and a clear restock timestamp. Respect Discord HTTP failure responses, record delivery health, and do not leak webhook URLs.

## Web Experience

1. Design a responsive, dark-forward, polished dashboard with restrained branded color, strong typography, product imagery, and clear available/unavailable/unknown/error status treatments. Preserve the same visual system on desktop and mobile instead of treating mobile as a compressed table.
2. Build public sign-up and sign-in pages, followed by an onboarding empty state that makes the three-step workflow explicit: choose Nintendo Canada, paste a link, review validated product, then select a Discord destination and start monitoring.
3. Build the monitor dashboard with summary metrics, filterable product cards, retailer badge, product image/title/price, live status, last-check time, Discord destination, and concise controls for pause, resume, edit destination, and delete. Make status polling modest and visible rather than claiming real-time streams.
4. Build a dedicated add-monitor flow with store picker, URL field, loading/error states, product-preview confirmation panel, webhook selection/creation, and explicit start action. Give unsupported retailer cards an honest coming-soon state.
5. Build settings for profile/password management and named Discord webhooks, including masked URL display, explicit test delivery, delivery result feedback, and deletion confirmation.
6. Build a staff-only custom administration area for pending-account review, user search, approve/reject/reactivate actions, monitor/product inspection, latest check errors, notification-delivery failures, scheduler health, and supported-store status. Approval changes the account to active/approved; rejection revokes access. Protect both the route and API endpoints with staff permissions, and prevent an administrator from removing the final active superuser.
7. Include accessible labels, visible keyboard focus, semantic controls, responsive layouts, empty/loading/error states, and human-readable timestamps. Do not rely on color alone for availability/error meaning.

## Containerization and Configuration

1. Add a multi-stage Dockerfile: install/build the React application with Node, install Python production dependencies, install Playwright Chromium and its Linux dependencies, copy compiled static assets, collect Django static files, and run as a non-root user.
2. Add `docker-compose.yml` with one `app` service built from the image, port mapping configurable through environment, persistent `stockbot_data` volume mounted at `/data`, a health check endpoint, restart policy, and an env-file based configuration pattern. It must not add Redis, Celery, or a separate worker service.
3. Add a `.env.example` containing non-secret placeholders and descriptions for `DJANGO_SECRET_KEY`, `DJANGO_DEBUG`, `ALLOWED_HOSTS`, `DATABASE_PATH`, `WEBHOOK_ENCRYPTION_KEY`, scheduler enablement, check cadence, Nintendo concurrency/timeout, and log level. Keep actual `.env` and SQLite files ignored.
4. Document in the project README the single-container limitation, persistent-volume backup procedure, first-registration administrator bootstrap and subsequent approval flow, reverse-proxy/TLS expectation for public deployment, supported Nintendo Canada shipping behavior, Discord webhook setup, and retailer terms/blocking limitation.

## Testing and Verification

1. Add Django model/API tests for account isolation, first-registration atomic superuser bootstrap, later pending registration and approval/rejection behavior, login/session CSRF behavior, validation expiry and ownership, encrypted/masked webhook handling, monitor CRUD, staff authorization, and alert re-arm/deduplication behavior.
2. Add adapter unit tests with saved Nintendo fixture responses and mocked Playwright pages covering valid product extraction, available/unavailable interpretation, product removed, malformed URLs, changed markup, CAPTCHA/access denial, and timeout/error classification.
3. Add scheduler tests for 60-second due selection, per-domain serialization, jitter bounds, no duplicate overlapping runs, retry/backoff behavior, history pruning, and heartbeat reporting.
4. Add notification tests using a mocked Discord endpoint for explicit tests, successful restock embeds, non-2xx failures, and guarantee that raw webhook values never appear in API responses or logs.
5. Add React component/integration tests for authentication screens, store catalogue states, validation preview and confirmation, monitor state rendering, webhook test feedback, responsive navigation, and staff route protection.
6. Run backend tests, frontend type-check/lint/test commands, build the Docker image, start Compose with test secrets, verify the health endpoint and static SPA fallback, register the first account and confirm it is staff, register a second account and approve it from the staff dashboard, validate a mocked Nintendo product, create a monitor, exercise a simulated unavailable-to-available notification, and confirm volume-backed SQLite persistence after container restart.

## Risks and Explicit Deferrals

- Nintendo Canada page structure, stock semantics, and bot defenses can change. The adapter must surface `blocked`/`error` instead of false stock claims; operational fixes will be adapter-specific.
- Unlimited accounts and monitor counts, plus intentionally absent signup abuse controls, can exhaust the single-container browser/scheduler capacity. The per-domain serialization and error states reduce retailer harm but do not provide service-abuse protection.
- SQLite and an embedded scheduler are deliberate single-instance constraints. A later scale-out design requires moving checks to a durable queue/worker and migrating the database to PostgreSQL.
- Store pickup, postal-code/store choice, price thresholds, email/SMS/browser notifications, password recovery, email verification, OAuth, CAPTCHA/rate limiting, and production adapters for Walmart, Best Buy, and Amazon are out of scope for this first implementation.
