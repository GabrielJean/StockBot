# StockBot

A self-hosted Nintendo Canada shipping-stock monitor with a responsive web dashboard, Discord delivery, public account registration, and administrator approval.

## Run with Docker

1. Create the runtime environment file: `cp .env.example .env`.
2. Generate a valid Fernet key for `WEBHOOK_ENCRYPTION_KEY` with:
   `docker compose run --rm app python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`
3. Set long random values for `DJANGO_SECRET_KEY` and `WEBHOOK_ENCRYPTION_KEY` in `.env`.
4. Start the application: `docker compose up --build -d`.
5. Open `http://localhost:8000`.

The first account registered becomes the initial StockBot administrator. Every later account is pending until an administrator approves it from **Admin control** in the dashboard.

## Monitoring behavior

- Nintendo Canada product links (`nintendo.com/en-ca`) are the only supported links in this release.
- StockBot validates a product before it creates a monitor.
- Checks run every 60 seconds by default and only cover online shipping availability.
- A monitor sends one Discord alert when stock transitions from unavailable to available. It re-arms once the product is confirmed unavailable again.
- Walmart Canada, Best Buy Canada, and Amazon Canada appear as planned stores but do not accept product URLs yet.

## Deployment notes

The Compose configuration deliberately has one application container with an embedded scheduler and SQLite database. Do not scale this service to multiple replicas. For public deployments, place it behind a TLS reverse proxy, set `ALLOWED_HOSTS`, `CSRF_TRUSTED_ORIGINS`, and `COOKIE_SECURE=true`, then keep the Docker volume backed up.

The SQLite database persists in the `stockbot_data` Docker volume. Back it up while the container is stopped, or use SQLite's backup facility. Discord webhook URLs are encrypted at rest using `WEBHOOK_ENCRYPTION_KEY`; losing that key makes existing webhook records unreadable.

Nintendo page structure and retailer access controls can change. Access denial, CAPTCHA-like responses, and page parsing failures are shown as errors rather than treated as out of stock. StockBot does not bypass retailer protections.

Set `STOCKBOT_USER_AGENT` in `.env` to choose the transparent identifier sent with all retailer requests, for example `StockBot/1.0 (+self-hosted inventory monitor; contact: ops@example.com)`. Keep it accurate and do not use it to impersonate a browser or bypass retailer protections.

## Development

Install the Python dependencies, then run `python manage.py migrate` and `python manage.py runserver`. Build the UI with `cd frontend && npm install && npm run build`. Run backend tests with `python manage.py test`.
