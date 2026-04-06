#!/bin/bash
# cloud-run/streamlit/deploy.sh — Deploy Streamlit frontend to Cloud Run

set -e

PROJECT_ID=${1:-$(gcloud config get-value project)}
REGION=${2:-asia-south1}
BACKEND_URL=${3:-""}
SERVICE_NAME="propus-frontend"

if [ -z "$PROJECT_ID" ]; then
    echo "Error: PROJECT_ID required"
    echo "Usage: ./deploy.sh PROJECT_ID [REGION] [BACKEND_URL]"
    exit 1
fi

echo "🚀 Deploying Streamlit Frontend"
echo "Project: $PROJECT_ID"
echo "Region: $REGION"
echo ""

# Build and deploy
echo "Building and deploying..."

if [ -z "$BACKEND_URL" ]; then
    # Deploy without BACKEND_URL (user will set it manually)
    gcloud run deploy $SERVICE_NAME \
        --source . \
        --platform managed \
        --region $REGION \
        --memory 2Gi \
        --cpu 2 \
        --timeout 3600 \
        --allow-unauthenticated \
        --project $PROJECT_ID
else
    # Deploy with BACKEND_URL
    gcloud run deploy $SERVICE_NAME \
        --source . \
        --platform managed \
        --region $REGION \
        --memory 2Gi \
        --cpu 2 \
        --timeout 3600 \
        --allow-unauthenticated \
        --set-env-vars BACKEND_URL=$BACKEND_URL \
        --project $PROJECT_ID
fi

# Get service URL
SERVICE_URL=$(gcloud run services describe $SERVICE_NAME \
    --region $REGION \
    --format 'value(status.url)' \
    --project $PROJECT_ID)

echo ""
echo "✅ Streamlit Frontend Deployed!"
echo "🎨 Service URL: $SERVICE_URL"
echo ""
echo "If you haven't set BACKEND_URL, update it now:"
echo "gcloud run services update $SERVICE_NAME \\"
echo "  --set-env-vars BACKEND_URL=https://propus-backend-xxxxx.run.app \\"
echo "  --region $REGION"
