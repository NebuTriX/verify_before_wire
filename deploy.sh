#!/usr/bin/env bash
# One-shot setup + deploy to Google Cloud Run + Firebase (Hosting, Authentication, Firestore). Safe to re-run.
#   PROJECT_ID=my-project FIREBASE_API_KEY=<web app apiKey> REGION=asia-southeast1 ./deploy.sh
# Needs: gcloud (logged in), and Node.js for the Firebase CLI (run through npx). Set FIREBASE_HOSTING=0
# to skip Firebase Hosting and use the Cloud Run URL directly.
set -euo pipefail
: "${PROJECT_ID:?set PROJECT_ID}"
: "${FIREBASE_API_KEY:?set FIREBASE_API_KEY (Firebase console > Project settings > Web app config)}"
REGION="${REGION:-asia-southeast1}"
SERVICE="${SERVICE:-verify-before-wire}"
FIREBASE_HOSTING="${FIREBASE_HOSTING:-1}"
# With Firebase Hosting, sign-in runs on the app's own domain (Hosting serves /__/auth/*), which
# avoids third-party-cookie problems in Safari and Chrome.
if [[ "$FIREBASE_HOSTING" == "1" ]]; then DEFAULT_AUTH_DOMAIN="$PROJECT_ID.web.app"; else DEFAULT_AUTH_DOMAIN="$PROJECT_ID.firebaseapp.com"; fi
SA="vbw-runtime@${PROJECT_ID}.iam.gserviceaccount.com"
BUCKET="${BUCKET:-${PROJECT_ID}-vbw-uploads}"

gcloud config set project "$PROJECT_ID" >/dev/null
echo "› enabling APIs"
gcloud services enable run.googleapis.com aiplatform.googleapis.com firestore.googleapis.com \
  storage.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com secretmanager.googleapis.com \
  identitytoolkit.googleapis.com firebase.googleapis.com

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
# --no-cpu-throttling + min 1 instance (below) keep the in-process worker running API-submitted cases
# and delivering webhooks between requests; Cloud Run would otherwise freeze the CPU when idle.
gcloud run deploy "$SERVICE" --source . --region "$REGION" --service-account "$SA" \
  --allow-unauthenticated --timeout 300 --cpu 1 --memory 2Gi --concurrency 40 \
  --min-instances 1 --max-instances 10 --no-cpu-throttling \
  --set-env-vars "GOOGLE_CLOUD_PROJECT=$PROJECT_ID,STORE_BACKEND=firestore,UPLOADS_BUCKET=$BUCKET,GEMINI_LOCATION=${GEMINI_LOCATION:-global},GEMINI_MODEL=${GEMINI_MODEL:-gemini-3-flash-preview},AUTH_MODE=firebase,DEMO_ENABLED=${DEMO_ENABLED:-true},BLOB_BACKEND=gcs,FIREBASE_PROJECT_ID=$PROJECT_ID,FIREBASE_API_KEY=$FIREBASE_API_KEY,FIREBASE_AUTH_DOMAIN=${FIREBASE_AUTH_DOMAIN:-$DEFAULT_AUTH_DOMAIN},GEMINI_MODE=vertex,SANCTIONS_CACHE_DIR=/tmp/sanctions" \
  "${SECRET_FLAG[@]}"

URL=$(gcloud run services describe "$SERVICE" --region "$REGION" --format='value(status.url)')
echo "› Cloud Run: $URL"
curl -s "$URL/readyz"; echo

if [[ "$FIREBASE_HOSTING" == "1" ]]; then
  echo "› Firebase: Hosting (rewrites to $SERVICE in $REGION) + Firestore rules and indexes"
  # keep firebase.json pointing at this service and region
  sed -i.bak -E "s/\"serviceId\": \"[^\"]+\"/\"serviceId\": \"$SERVICE\"/; s/\"region\": \"[^\"]+\"/\"region\": \"$REGION\"/" firebase.json && rm -f firebase.json.bak
  npx -y firebase-tools@latest deploy --project "$PROJECT_ID" --only hosting,firestore:rules,firestore:indexes --non-interactive
  echo "› live at https://$PROJECT_ID.web.app"
  curl -s "https://$PROJECT_ID.web.app/readyz"; echo
fi
echo "› Sign-in: enable Google and Email/Password in Firebase console > Authentication > Sign-in method."
echo "  Authorized domains already include $PROJECT_ID.web.app and $PROJECT_ID.firebaseapp.com; add the Cloud Run URL if you use it directly."
echo "  Check the setup any time with: python backend/scripts/firebase_check.py"
