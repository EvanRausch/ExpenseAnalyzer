#!/usr/bin/env bash
# deploy/cloud.sh — put the server on Google Cloud, one step at a time.
#
# Usage (in this order the first time):
#   ./deploy/cloud.sh setup       sign this Mac in to Google Cloud and prepare the project
#   ./deploy/cloud.sh secrets     copy the keys from .env into Secret Manager (values never shown)
#   ./deploy/cloud.sh deploy      build the server and put it live on Cloud Run
#   ./deploy/cloud.sh import-mac  copy this Mac's data into the live database (once)
#
# Afterwards:
#   ./deploy/cloud.sh token [id]  print a fresh sign-in token (lists the accounts when there are
#                                 several; give the first characters of the one you mean)
#   ./deploy/cloud.sh add-user    create an empty account for another person; prints their token
#   ./deploy/cloud.sh recategorise have Gemini categorise everyone's transactions again (two-level list)
#   ./deploy/cloud.sh sync <id>    fetch an account's new bank transactions now (first characters of its id)
#   ./deploy/cloud.sh deploy      again, whenever server/ or src/ changed
#   ./deploy/cloud.sh url         print the server's address (for the iPhone app and Plaid)
#   ./deploy/cloud.sh logs        show the server's latest log lines
#   ./deploy/cloud.sh status      show whether the server answers
#
# Every gcloud command uses the "expenseanalyzer" profile, never the default one.

set -euo pipefail

PROJECT="places-project-budget-tracker"
PROJECT_NUMBER="1049746016384"
REGION="northamerica-northeast1"      # Montreal
SERVICE="expenseanalyzer"
SQL_INSTANCE="expenseanalyzer"
CONNECTION="$PROJECT:$REGION:$SQL_INSTANCE"
RUNNER="expenseanalyzer-run"           # the identity the server runs as
RUNNER_EMAIL="$RUNNER@$PROJECT.iam.gserviceaccount.com"
URL="https://$SERVICE-$PROJECT_NUMBER.$REGION.run.app"
BUNDLE_ID="EvanRL.ExpenseAnalyzer"
# gemini-2.5-flash, the code's default, only allows 20 requests a day on the free tier
GEMINI_MODEL="${GEMINI_MODEL:-gemini-3.5-flash}"
PROFILE="expenseanalyzer"

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PYTHON="${PYTHON:-$HOME/miniconda3/envs/budgettracker/bin/python}"
PROXY_VERSION="v2.18.0"
PROXY="$ROOT/deploy/bin/cloud-sql-proxy"
PROXY_PORT=5433

# Secret Manager name -> the setting the server reads it as
SECRETS=(
  "expense-database-url:DATABASE_URL"
  "expense-secret-key:EXPENSE_SECRET_KEY"
  "expense-plaid-client-id:PLAID_CLIENT_ID"
  "expense-plaid-secret:PLAID_SECRET"
  "expense-gemini-api-key:GEMINI_API_KEY"
  "expense-places-api-key:GOOGLE_PLACES_API_KEY"
)

export CLOUDSDK_ACTIVE_CONFIG_NAME="$PROFILE"   # for tools that call gcloud themselves
g() { gcloud --configuration="$PROFILE" "$@"; }

say() { printf '\n▶ %s\n' "$*"; }
ok() { printf '✓ %s\n' "$*"; }

# The value of a setting in .env, without echoing it anywhere; empty when absent
env_value() {
  { grep -E "^$1=" "$ROOT/.env" 2>/dev/null || true; } | head -1 | cut -d= -f2- \
    | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//' -e 's/^"\(.*\)"$/\1/' -e "s/^'\(.*\)'$/\1/"
}

secret_exists() { g secrets describe "$1" >/dev/null 2>&1; }

# Store a value under a secret name, adding a version only if it changed
store_secret() {
  local name="$1" value="$2"
  if secret_exists "$name"; then
    if [ "$(g secrets versions access latest --secret="$name" 2>/dev/null)" = "$value" ]; then
      ok "$name unchanged"
    else
      printf '%s' "$value" | g secrets versions add "$name" --data-file=- >/dev/null
      ok "$name updated"
    fi
  else
    printf '%s' "$value" | g secrets create "$name" --data-file=- \
      --replication-policy=user-managed --locations="$REGION" >/dev/null
    ok "$name created"
  fi
  g secrets add-iam-policy-binding "$name" \
    --member="serviceAccount:$RUNNER_EMAIL" --role="roles/secretmanager.secretAccessor" >/dev/null
}

need_login() {
  if ! g auth list --filter=status:ACTIVE --format='value(account)' 2>/dev/null | grep -q .; then
    echo "This Mac is not signed in to Google Cloud yet. Run: ./deploy/cloud.sh setup" >&2
    exit 1
  fi
}

# Start the Cloud SQL Auth Proxy on PROXY_PORT; stopped when the script ends
open_tunnel() {
  if [ ! -x "$PROXY" ]; then
    say "Downloading the Cloud SQL Auth Proxy (connects this Mac to the database safely)"
    mkdir -p "$(dirname "$PROXY")"
    curl -fsSL -o "$PROXY" \
      "https://storage.googleapis.com/cloud-sql-connectors/cloud-sql-proxy/$PROXY_VERSION/cloud-sql-proxy.darwin.$(uname -m)"
    chmod +x "$PROXY"
  fi
  say "Opening a tunnel to the database"
  mkdir -p "$ROOT/logs"
  "$PROXY" --gcloud-auth --port "$PROXY_PORT" "$CONNECTION" >"$ROOT/logs/cloud-sql-proxy.log" 2>&1 &
  proxy_pid=$!
  trap 'kill $proxy_pid 2>/dev/null || true' EXIT
  for _ in $(seq 1 30); do
    nc -z 127.0.0.1 "$PROXY_PORT" 2>/dev/null && break
    sleep 1
  done
  nc -z 127.0.0.1 "$PROXY_PORT" 2>/dev/null || { echo "✗ tunnel did not open; see logs/cloud-sql-proxy.log" >&2; exit 1; }
  ok "tunnel open"
}

# Run a command with the server's database settings, through the tunnel (values never shown)
with_database() {
  local database_url secret_key
  # The same address the server uses, pointed at the tunnel instead of the socket
  database_url="$(g secrets versions access latest --secret=expense-database-url | "$PYTHON" -c '
import sys
from sqlalchemy.engine import make_url
url = make_url(sys.stdin.read().strip())
query = {k: v for k, v in url.query.items() if k != "host"}
print(url.set(host="127.0.0.1", port=int(sys.argv[1]), query=query).render_as_string(hide_password=False))
' "$PROXY_PORT")"
  secret_key="$(g secrets versions access latest --secret=expense-secret-key)"
  (
    cd "$ROOT"
    DATABASE_URL="$database_url" EXPENSE_SECRET_KEY="$secret_key" \
    PLAID_ENV=production PLAID_WEBHOOK_URL="$URL/plaid/webhook" "$@"
  )
}

case "${1:-}" in

  setup)
    say "Google Cloud profile '$PROFILE'"
    if ! gcloud config configurations describe "$PROFILE" >/dev/null 2>&1; then
      gcloud config configurations create "$PROFILE" --no-activate >/dev/null
      ok "profile created"
    fi
    if ! g auth list --filter=status:ACTIVE --format='value(account)' | grep -q .; then
      echo "A browser window will open: sign in with evanrauschl@gmail.com."
      g auth login
    fi
    g config set project "$PROJECT" >/dev/null
    g config set run/region "$REGION" >/dev/null
    ok "signed in as $(g config get-value account)"

    say "Services"
    g services enable run.googleapis.com sqladmin.googleapis.com secretmanager.googleapis.com \
      cloudbuild.googleapis.com artifactregistry.googleapis.com
    ok "switched on"

    say "Identity the server runs as ($RUNNER)"
    if ! g iam service-accounts describe "$RUNNER_EMAIL" >/dev/null 2>&1; then
      g iam service-accounts create "$RUNNER" --display-name="ExpenseAnalyzer server" >/dev/null
    fi
    g projects add-iam-policy-binding "$PROJECT" --member="serviceAccount:$RUNNER_EMAIL" \
      --role="roles/cloudsql.client" --condition=None >/dev/null
    # Cloud Build builds the server with the project's default compute identity
    g projects add-iam-policy-binding "$PROJECT" \
      --member="serviceAccount:$PROJECT_NUMBER-compute@developer.gserviceaccount.com" \
      --role="roles/cloudbuild.builds.builder" --condition=None >/dev/null
    ok "ready"
    echo
    echo "Next: ./deploy/cloud.sh secrets"
    ;;

  secrets)
    need_login
    [ -f "$ROOT/.env" ] || { echo ".env not found in $ROOT" >&2; exit 1; }
    say "Copying keys from .env to Secret Manager (values are never displayed)"
    for pair in "${SECRETS[@]}"; do
      name="${pair%%:*}"; setting="${pair##*:}"
      value="$(env_value "$setting")"
      case "$setting" in
        DATABASE_URL)
          # Created when the database was set up; .env normally has no value for it
          if [ -n "$value" ]; then store_secret "$name" "$value"
          elif secret_exists "$name"; then
            g secrets add-iam-policy-binding "$name" --member="serviceAccount:$RUNNER_EMAIL" \
              --role="roles/secretmanager.secretAccessor" >/dev/null
            ok "$name kept (created earlier)"
          else
            echo "✗ $name is missing and .env has no $setting" >&2; exit 1
          fi
          ;;
        EXPENSE_SECRET_KEY)
          if [ -n "$value" ]; then store_secret "$name" "$value"
          elif secret_exists "$name"; then
            g secrets add-iam-policy-binding "$name" --member="serviceAccount:$RUNNER_EMAIL" \
              --role="roles/secretmanager.secretAccessor" >/dev/null
            ok "$name kept (generated earlier; it must never change once banks are stored)"
          else
            store_secret "$name" "$("$PYTHON" -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())')"
          fi
          ;;
        GOOGLE_PLACES_API_KEY)
          # Optional: without it transactions simply get no place details
          if [ -n "$value" ]; then store_secret "$name" "$value"
          elif secret_exists "$name"; then ok "$name kept"
          else echo "  (no $setting in .env, skipped; add one later and run this step again)"
          fi
          ;;
        *)
          [ -n "$value" ] || { echo "✗ $setting is empty in .env" >&2; exit 1; }
          store_secret "$name" "$value"
          ;;
      esac
    done

    say "Checking the shape of the database address (password hidden)"
    g secrets versions access latest --secret=expense-database-url | "$PYTHON" -c '
import sys
from sqlalchemy.engine import make_url
url = make_url(sys.stdin.read().strip())
socket = f"/cloudsql/{sys.argv[1]}"
host, via = url.host or "", url.query.get("host", "")
print(f"  {url.drivername}://{url.username}:***@{host}/{url.database}?host={via}")
if via == socket and not host:
    print("  ✓ connects through the Cloud SQL socket, as Cloud Run needs")
else:
    print(f"  ✗ expected no host and ?host={socket}. Fix the secret before deploying.")
    sys.exit(1)
' "$CONNECTION"
    echo
    echo "Next: ./deploy/cloud.sh deploy"
    ;;

  deploy)
    need_login
    say "Files that will be uploaded (see .gcloudignore)"
    (cd "$ROOT" && gcloud meta list-files-for-upload . | sed 's/^/  /')
    if (cd "$ROOT" && gcloud meta list-files-for-upload .) | grep -qE '^(data/|\.env|docs/)'; then
      echo "✗ Private files would be uploaded. Stopping." >&2; exit 1
    fi
    set_secrets=""
    for pair in "${SECRETS[@]}"; do
      # Only secrets that exist; the Places key is optional
      secret_exists "${pair%%:*}" && set_secrets="$set_secrets,${pair##*:}=${pair%%:*}:latest"
    done
    say "Building and deploying $SERVICE in $REGION (a few minutes)"
    g run deploy "$SERVICE" --quiet \
      --source "$ROOT" \
      --region "$REGION" \
      --service-account "$RUNNER_EMAIL" \
      --allow-unauthenticated \
      --no-cpu-throttling \
      --min-instances 0 --max-instances 1 --concurrency 20 \
      --cpu 1 --memory 1Gi --timeout 300 \
      --add-cloudsql-instances "$CONNECTION" \
      --set-env-vars "PLAID_ENV=production,APPLE_BUNDLE_ID=$BUNDLE_ID,PLAID_WEBHOOK_URL=$URL/plaid/webhook,GEMINI_MODEL=$GEMINI_MODEL" \
      --set-secrets "${set_secrets#,}"
    say "Checking"
    if curl -fsS --max-time 30 "$URL/health" >/dev/null; then
      ok "the server answers at $URL"
    else
      echo "✗ $URL/health did not answer. See: ./deploy/cloud.sh logs" >&2; exit 1
    fi
    ;;

  import-mac)
    need_login
    [ -f "$ROOT/data/enriched_transactions.csv" ] || { echo "No data/enriched_transactions.csv to import" >&2; exit 1; }
    open_tunnel
    say "Importing data/ into the live database"
    with_database "$PYTHON" -m server.cli import-mac
    echo
    echo "The Token line above is what the iPhone app asks for on its sign-in screen."
    echo "It is shown once; copy it now (it is not stored anywhere on this Mac)."
    echo "Lost it? ./deploy/cloud.sh token prints a new one."
    ;;

  token)
    need_login
    open_tunnel
    say "New sign-in token"
    with_database "$PYTHON" -m server.cli new-token ${2:+--account "$2"}
    echo
    echo "Paste it on the iPhone app's sign-in screen. Earlier tokens keep working, and"
    echo "signing out of the app does not cancel them."
    ;;

  add-user)
    need_login
    open_tunnel
    say "New account"
    with_database "$PYTHON" -m server.cli add-user
    echo
    echo "That person pastes the token on the app's sign-in screen, then uses"
    echo "Connect a Bank… in the sync menu to log in to their bank on Plaid's page."
    ;;

  sync)
    need_login
    [ -n "${2:-}" ] || { echo "Which account? ./deploy/cloud.sh sync <first characters of the id>" >&2; exit 1; }
    open_tunnel
    say "Fetching the bank's transactions"
    with_database "$PYTHON" -m server.cli sync --account "$2"
    ;;

  recategorise)
    need_login
    open_tunnel
    say "Categorising every transaction again with the two-level list (a few minutes)"
    with_database "$PYTHON" -m server.cli recategorise
    ;;

  url)
    echo "$URL"
    ;;

  status)
    if curl -fsS --max-time 30 "$URL/health" >/dev/null 2>&1; then
      ok "the server answers at $URL"
    else
      echo "✗ the server does not answer at $URL"
      exit 1
    fi
    ;;

  logs)
    need_login
    g run services logs read "$SERVICE" --region "$REGION" --limit "${2:-50}"
    ;;

  *)
    sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'
    exit 1
    ;;
esac
