#!/bin/bash
# cloud-run/fastapi/deploy.sh — Deploy FastAPI backend to Cloud Run

set -e

PROJECT_ID=${1:-$(gcloud config get-value project)}
REGION=${2:-asia-south1}
SERVICE_NAME="propus-backend"

if [ -z "$PROJECT_ID" ]; then
    echo "❌ Error: PROJECT_ID required"
    echo "Usage: ./deploy.sh PROJECT_ID [REGION]"
    exit 1
fi

echo "🚀 Deploying FastAPI Backend"
echo "Project: $PROJECT_ID"
echo "Region: $REGION"
echo ""

# Verify we're using the right requirements file
echo "📋 Checking requirements file..."
if [ -f "requirements-cloud.txt" ]; then
    echo "✅ Found requirements-cloud.txt"
    echo "   Using this file for dependencies"
else
    echo "❌ Error: requirements-cloud.txt not found!"
    exit 1
fi

# Build and deploy
echo ""
echo "🔨 Building and deploying Docker image..."
gcloud run deploy $SERVICE_NAME \
    --source . \
    --platform managed \
    --region $REGION \
    --memory 4Gi \
    --cpu 4 \
    --timeout 3600 \
    --allow-unauthenticated \
    --project $PROJECT_ID \
    --quiet

# Get service URL
SERVICE_URL=$(gcloud run services describe $SERVICE_NAME \
    --region $REGION \
    --format 'value(status.url)' \
    --project $PROJECT_ID)

echo ""
echo "✅ FastAPI Backend Deployed!"
echo "📡 Service URL: $SERVICE_URL"
echo ""
echo "🏥 Checking health..."
sleep 3
curl -s "${SERVICE_URL}/health" || echo "⏳ Service still warming up, check logs:"
echo "   gcloud run logs read $SERVICE_NAME --region $REGION --limit 20"
