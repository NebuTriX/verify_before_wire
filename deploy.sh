#!/usr/bin/env bash
# One-shot setup + deploy to Cloud Run. Safe to re-run.
#   PROJECT_ID=my-project REGION=asia-southeast1 ./deploy.sh
set -euo pipefail
: "${PROJECT_ID:?set PROJECT_ID}"
REGION="${REGION:-asia-southeast1}"
SERVICE="${SERVICE:-verify-before-wire}"
SA="vbw-runtime@${PROJECT_ID}.iam.gserviceaccount.com"
BUCKET="${BUCKET:-${PROJECT_ID}-vbw-uploads}"

gcloud config set project "$PROJECT_ID" >/dev/null
echo "› enabling APIs"
gcloud services enable run.googleapis.com aiplatform.googleapis.com firestore.googleapis.com \
  storage.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com secretmanager.googleapis.com

echo "› Firestore (Native mode)"
gcloud firestore databases describe --database="(default)" >/dev/null 2>&1 || \
  gcloud firestore databases create --database="(default)" --location="$REGION" --type=firestore-native

echo "› uploads bucket (objects deleted after 7 days)"
gcloud storage buckets describe "gs://$BUCKET" >/dev/null 2>&1 || \
  gcloud storage buckets create "gs://$BUCKET" --location="$REGION" --uniform-bucket-level-access
echo '{"rule":[{"action":{"type":"Delete"},"condition":{"age":7}}]}' > /tmp/vbw-lifecycle.json
gcloud storage buckets update "gs://$BUCKET" --lifecycle-file=/tmp/vbw-lifecycle.json >/dev/null

echo "› runtime service account"
gcloud iam service-accounts describe "$SA" >/dev/null 2>&1 || \
  gcloud iam service-accounts create vbw-runtime --display-name="Verify-Before-Wire runtime"
for role in roles/aiplatform.user roles/datastore.user roles/logging.logWriter; do
  gcloud projects add-iam-policy-binding "$PROJECT_ID" --member="serviceAccount:$SA" \
    --role="$role" --condition=None >/dev/null
done
gcloud storage buckets add-iam-policy-binding "gs://$BUCKET" --member="serviceAccount:$SA" \
  --role=roles/storage.objectAdmin >/dev/null

SECRET_FLAG=()
if [[ -n "${OPENSANCTIONS_API_KEY:-}" ]]; then
  echo "› OpenSanctions key -> Secret Manager"
  gcloud secrets describe opensanctions-key >/dev/null 2>&1 || \
    gcloud secrets create opensanctions-key --replication-policy=automatic
  printf '%s' "$OPENSANCTIONS_API_KEY" | gcloud secrets versions add opensanctions-key --data-file=- >/dev/null
  gcloud secrets add-iam-policy-binding opensanctions-key --member="serviceAccount:$SA" \
    --role=roles/secretmanager.secretAccessor >/dev/null
  SECRET_FLAG=(--set-secrets=OPENSANCTIONS_API_KEY=opensanctions-key:latest)
fi

echo "› deploying $SERVICE (Cloud Build builds the Dockerfile)"
gcloud run deploy "$SERVICE" --source . --region "$REGION" --service-account "$SA" \
  --allow-unauthenticated --timeout 300 --cpu 1 --memory 1Gi --concurrency 40 \
  --min-instances 1 --max-instances 4 --no-cpu-throttling \
  --set-env-vars "GOOGLE_CLOUD_PROJECT=$PROJECT_ID,STORE_BACKEND=firestore,UPLOADS_BUCKET=$BUCKET,GEMINI_LOCATION=${GEMINI_LOCATION:-global},GEMINI_MODEL=${GEMINI_MODEL:-gemini-3-flash-preview},COMPANY_DOMAINS=${COMPANY_DOMAINS:-acme-holdings.com}" \
  "${SECRET_FLAG[@]}"

URL=$(gcloud run services describe "$SERVICE" --region "$REGION" --format='value(status.url)')
echo "› live at $URL"
curl -s "$URL/healthz"; echo
