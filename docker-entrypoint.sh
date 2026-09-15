#!/bin/sh
set -eu

runtime_env="${STOCKBOT_RUNTIME_ENV:-/data/stockbot.env}"

create_runtime_env() {
  umask 077
  django_secret="${DJANGO_SECRET_KEY:-}"
  fernet_key="${WEBHOOK_ENCRYPTION_KEY:-}"
  if [ -z "$django_secret" ]; then
    django_secret=$(python -c "import secrets; print(secrets.token_urlsafe(50))")
  fi
  if [ -z "$fernet_key" ]; then
    fernet_key=$(python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())")
  fi
  cat > "$runtime_env" <<EOF
DJANGO_SECRET_KEY=$django_secret
WEBHOOK_ENCRYPTION_KEY=$fernet_key
EOF
}

if [ ! -f "$runtime_env" ]; then
  create_runtime_env
fi

. "$runtime_env"
export DJANGO_SECRET_KEY WEBHOOK_ENCRYPTION_KEY

exec "$@"
