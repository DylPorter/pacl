#!/usr/bin/env bash
set -euo pipefail

# Configuration (override via env)
PROJECT_ID="${GCP_PROJECT:?set GCP_PROJECT}"
REGION="${GCP_REGION:-us-central1}"
SERVICE="${PACL_SERVICE:-pacl}"

echo "Deploying ${SERVICE} to ${PROJECT_ID}/${REGION}"

# Substrate is local-disk on the running instance; no bucket needed.
# Durable/shared storage is future work.

# Build and push image
IMAGE="gcr.io/${PROJECT_ID}/${SERVICE}:latest"
gcloud builds submit --project="${PROJECT_ID}" --tag "${IMAGE}" .

# min-instances=1 avoids cold starts. Agnostic mode on gemini-2.5-pro (GA — the
# 3.x previews intermittently 503, and agnostic mode has no fallback). 2Gi memory:
# the ADK + Phoenix stack needs more than the 512MiB default just to import.
gcloud run deploy "${SERVICE}" \
  --project "${PROJECT_ID}" \
  --image "${IMAGE}" \
  --region "${REGION}" \
  --platform managed \
  --allow-unauthenticated \
  --min-instances 1 \
  --max-instances 3 \
  --memory 2Gi \
  --cpu 1 \
  --timeout 3600 \
  --set-env-vars "GEMINI_MODEL=gemini-2.5-pro,PACL_MODE=agnostic,PHOENIX_PROJECT=pacl-demo,PHOENIX_COLLECTOR_ENDPOINT=https://app.phoenix.arize.com" \
  --set-secrets "GEMINI_API_KEY=gemini-api-key:latest,PHOENIX_API_KEY=phoenix-api-key:latest"

echo ""
echo "deployed at:"
gcloud run services describe "${SERVICE}" --project "${PROJECT_ID}" --region "${REGION}" --format='value(status.url)'
