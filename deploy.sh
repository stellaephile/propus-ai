#!/bin/bash
# deploy.sh — Automated deployment script for Propus AI on GCP Cloud Run

set -e

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Configuration
PROJECT_ID=${1:-$(gcloud config get-value project)}
REGION=${2:-us-central1}
SERVICE_ACCOUNT="propus-sa@${PROJECT_ID}.iam.gserviceaccount.com"

if [ -z "$PROJECT_ID" ]; then
    echo -e "${RED}Error: GCP Project ID required${NC}"
    echo "Usage: ./deploy.sh [PROJECT_ID] [REGION]"
    exit 1
fi

echo -e "${YELLOW}🚀 Deploying Propus AI to GCP Cloud Run${NC}"
echo "Project: $PROJECT_ID"
echo "Region: $REGION"
echo ""

# Step 1: Create Service Account
echo -e "${YELLOW}1️⃣  Creating service account...${NC}"
gcloud iam service-accounts create propus-sa \
    --display-name="Propus AI Service Account" \
    --project=$PROJECT_ID 2>/dev/null || echo "Service account already exists"

gcloud projects add-iam-policy-binding $PROJECT_ID \
    --member="serviceAccount:${SERVICE_ACCOUNT}" \
    --role="roles/run.admin" \
    --quiet 2>/dev/null || true

echo -e "${GREEN}✓ Service account ready${NC}"

# Step 2: Build and Push Images
echo -e "${YELLOW}2️⃣  Building and pushing Docker images...${NC}"

echo "  Building FastAPI backend..."
gcloud builds submit \
    --config - \
    --project=$PROJECT_ID \
    <<EOF
steps:
  - name: 'gcr.io/cloud-builders/docker'
    args: ['build', '-f', 'Dockerfile.fastapi', '-t', 'gcr.io/${PROJECT_ID}/propus-fastapi:latest', '.']
  - name: 'gcr.io/cloud-builders/docker'
    args: ['push', 'gcr.io/${PROJECT_ID}/propus-fastapi:latest']
EOF

echo -e "${GREEN}✓ FastAPI image pushed${NC}"

echo "  Building Streamlit frontend..."
gcloud builds submit \
    --config - \
    --project=$PROJECT_ID \
    <<EOF
steps:
  - name: 'gcr.io/cloud-builders/docker'
    args: ['build', '-f', 'Dockerfile.streamlit', '-t', 'gcr.io/${PROJECT_ID}/propus-streamlit:latest', '.']
  - name: 'gcr.io/cloud-builders/docker'
    args: ['push', 'gcr.io/${PROJECT_ID}/propus-streamlit:latest']
EOF

echo -e "${GREEN}✓ Streamlit image pushed${NC}"

# Step 3: Deploy FastAPI Backend
echo -e "${YELLOW}3️⃣  Deploying FastAPI backend...${NC}"

gcloud run deploy propus-backend \
    --image gcr.io/${PROJECT_ID}/propus-fastapi:latest \
    --platform managed \
    --region $REGION \
    --memory 4Gi \
    --cpu 4 \
    --timeout 3600 \
    --allow-unauthenticated \
    --service-account $SERVICE_ACCOUNT \
    --project=$PROJECT_ID \
    --quiet

BACKEND_URL=$(gcloud run services describe propus-backend \
    --region $REGION \
    --format 'value(status.url)' \
    --project=$PROJECT_ID)

echo -e "${GREEN}✓ Backend deployed at: $BACKEND_URL${NC}"

# Step 4: Deploy Streamlit Frontend
echo -e "${YELLOW}4️⃣  Deploying Streamlit frontend...${NC}"

gcloud run deploy propus-frontend \
    --image gcr.io/${PROJECT_ID}/propus-streamlit:latest \
    --platform managed \
    --region $REGION \
    --memory 2Gi \
    --cpu 2 \
    --timeout 3600 \
    --allow-unauthenticated \
    --set-env-vars BACKEND_URL=${BACKEND_URL} \
    --service-account $SERVICE_ACCOUNT \
    --project=$PROJECT_ID \
    --quiet

FRONTEND_URL=$(gcloud run services describe propus-frontend \
    --region $REGION \
    --format 'value(status.url)' \
    --project=$PROJECT_ID)

echo -e "${GREEN}✓ Frontend deployed at: $FRONTEND_URL${NC}"

# Step 5: Verify Deployments
echo ""
echo -e "${YELLOW}5️⃣  Verifying deployments...${NC}"

sleep 5

echo -n "  Backend health: "
if curl -s "${BACKEND_URL}/health" > /dev/null 2>&1; then
    echo -e "${GREEN}✓ Healthy${NC}"
else
    echo -e "${RED}✗ Not responding${NC}"
fi

echo -n "  Frontend: "
if curl -s "$FRONTEND_URL" > /dev/null 2>&1; then
    echo -e "${GREEN}✓ Running${NC}"
else
    echo -e "${RED}✗ Not responding${NC}"
fi

# Summary
echo ""
echo -e "${GREEN}🎉 Deployment complete!${NC}"
echo ""
echo "Services deployed:"
echo -e "  📡 Backend:  ${BACKEND_URL}"
echo -e "  🎨 Frontend: ${FRONTEND_URL}"
echo ""
echo "View logs:"
echo "  gcloud run logs read propus-backend --region $REGION"
echo "  gcloud run logs read propus-frontend --region $REGION"
echo ""
echo "Manage services:"
echo "  gcloud run services list --region $REGION"
