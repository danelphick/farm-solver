#!/usr/bin/env bash
#
# Deploy the farm-solver web app to Google Cloud Run.
#
# Cloud Run fits this app well: it is stateless (no DB, no disk), bursty and
# CPU-bound, and idle almost all the time -- so per-request billing plus
# scale-to-zero means a small deployment sits inside the always-free monthly
# allowance (2M requests, 180k vCPU-seconds, 360k GiB-seconds).
#
# Usage:
#     ./deploy/cloudrun.sh                 # deploy with the defaults below
#     REGION=us-east1 ./deploy/cloudrun.sh
#     MAX_INSTANCES=5 ./deploy/cloudrun.sh
#
# Re-running is safe: it builds a new revision and keeps the generated
# DJANGO_SECRET_KEY from the existing service.

set -euo pipefail

cd "$(dirname "$0")/.."

SERVICE="${SERVICE:-farm-solver}"

# The always-free allowance covers only us-central1, us-east1 and us-west1.
# Deploying anywhere else is billed from the first request.
REGION="${REGION:-us-central1}"

PROJECT_ID="${PROJECT_ID:-$(gcloud config get-value project 2>/dev/null || true)}"
if [[ -z "${PROJECT_ID}" || "${PROJECT_ID}" == "(unset)" ]]; then
    echo "error: no project. Set PROJECT_ID=... or run 'gcloud config set project <id>'." >&2
    exit 1
fi

# A hard ceiling on how many instances can exist at once. This is the one knob
# that bounds a bad month: api_solve and api_mastery_costs are csrf_exempt and
# unauthenticated (there is no login to gate them), and each POST buys 1-3s of
# CPU, so anyone who finds the URL can spend your quota. Keep this small and add
# a billing budget alert -- see the notes at the bottom of this file.
MAX_INSTANCES="${MAX_INSTANCES:-3}"

# Requests routed to one instance before Cloud Run starts another. Sized against
# the app's own limits rather than raw throughput: SOLVE_SEMAPHORE lets one solve
# run per process at a time, and views._SEMAPHORE_TIMEOUT_SECONDS gives a queued
# request 20s to acquire it before returning 503. At 4, three ~1-3s solves can
# stack up behind one and still make it. Lower this if you see 503s.
CONCURRENCY="${CONCURRENCY:-4}"

# 512Mi against a measured ~66 MB peak RSS for a full frontier sweep, leaving
# room for the per-worker SessionCache (64 entries) and the interpreter.
MEMORY="${MEMORY:-512Mi}"
CPU="${CPU:-1}"

echo ">> project=${PROJECT_ID} service=${SERVICE} region=${REGION}"

# Reuse the existing secret key rather than rotating it on every deploy.
# settings.py wires up no sessions, auth, cookies or messages framework, so this
# key signs nothing that outlives a request -- an env var is proportionate here.
# To harden it anyway, put the key in Secret Manager and swap the
# --set-env-vars line below for:
#     --set-secrets "DJANGO_SECRET_KEY=farm-solver-secret-key:latest"
SECRET_KEY="$(
    gcloud run services describe "${SERVICE}" \
        --project "${PROJECT_ID}" --region "${REGION}" --format=json 2>/dev/null \
    | python3 -c '
import json, sys
try:
    spec = json.load(sys.stdin)["spec"]["template"]["spec"]
except Exception:
    sys.exit()
for container in spec.get("containers", []):
    for env in container.get("env", []):
        if env.get("name") == "DJANGO_SECRET_KEY" and env.get("value"):
            print(env["value"])
            sys.exit()
' || true
)"

if [[ -z "${SECRET_KEY}" ]]; then
    echo ">> generating a new DJANGO_SECRET_KEY"
    SECRET_KEY="$(python3 -c 'import secrets; print(secrets.token_urlsafe(64))')"
else
    echo ">> reusing the existing DJANGO_SECRET_KEY"
fi

# --source . builds the Dockerfile with Cloud Build and pushes it for you, so
# there is no registry to create or image tag to track by hand.
#
# --cpu-boost gives extra CPU during container start, which is what a
# scale-to-zero service pays on a cold request.
#
# --no-cpu-throttling is deliberately NOT set: this app does nothing between
# requests (its only background state is an in-memory cache), so paying for
# always-allocated CPU would buy nothing and would leave the free tier.
gcloud run deploy "${SERVICE}" \
    --project "${PROJECT_ID}" \
    --region "${REGION}" \
    --source . \
    --allow-unauthenticated \
    --cpu "${CPU}" \
    --memory "${MEMORY}" \
    --concurrency "${CONCURRENCY}" \
    --min-instances 0 \
    --max-instances "${MAX_INSTANCES}" \
    --timeout 120 \
    --cpu-boost \
    --set-env-vars "DJANGO_DEBUG=false,DJANGO_SECRET_KEY=${SECRET_KEY}"

# The image ships DJANGO_ALLOWED_HOSTS=.run.app so the first deploy can serve
# before its URL is known. Now that it is known, narrow it to the exact host.
URL="$(gcloud run services describe "${SERVICE}" \
    --project "${PROJECT_ID}" --region "${REGION}" --format='value(status.url)')"
HOST="${URL#https://}"

echo ">> pinning DJANGO_ALLOWED_HOSTS=${HOST}"
gcloud run services update "${SERVICE}" \
    --project "${PROJECT_ID}" --region "${REGION}" \
    --update-env-vars "DJANGO_ALLOWED_HOSTS=${HOST}" \
    --quiet >/dev/null

echo
echo "deployed: ${URL}"
echo
echo "Next, if you have not already:"
echo "  * Set a billing budget alert, since the service is public and CPU-bound:"
echo "      https://console.cloud.google.com/billing/budgets"
echo "  * Watch cold starts and solve latency:"
echo "      gcloud run services logs tail ${SERVICE} --region ${REGION}"
