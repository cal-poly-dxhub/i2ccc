#!/bin/bash

# Admin UI deployment: builds admin-frontend/ and uploads it to its own
# S3 bucket + CloudFront distribution (created by `cdk deploy`).
# Run `cdk deploy` first. Reads everything from the stack outputs.

set -e

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
ADMIN_DIR="$PROJECT_ROOT/admin-frontend"

STACK_NAME=$(grep -o '"RagChatbotStack[^"]*"' "$PROJECT_ROOT/cdk.json" | head -1 | tr -d '"' 2>/dev/null || true)
STACK_NAME="${STACK_NAME:-RagChatbotStack}"
echo "Using stack name: $STACK_NAME"

# "| [0]" takes the first match; a missing output prints "None", mapped to empty
get_output() {
    local value
    value=$(aws cloudformation describe-stacks \
        --stack-name "$STACK_NAME" \
        --query "Stacks[0].Outputs[?contains(OutputKey, '$1')].OutputValue | [0]" \
        --output text)
    [ "$value" = "None" ] && value=""
    printf '%s' "$value"
}

BUCKET_NAME=$(get_output AdminBucket)
DISTRIBUTION_ID=$(get_output AdminDistribution)
ADMIN_URL=$(get_output AdminURL)
ADMIN_URL="${ADMIN_URL%/}"
COGNITO_DOMAIN=$(get_output CognitoHostedUiDomain)
COGNITO_CLIENT_ID=$(get_output CognitoAppClientId)
# Empty on dev stacks (no SAML provider), which makes the hosted UI show the password form
COGNITO_IDP_NAME=$(get_output CognitoSamlIdpName)

if [ -z "$BUCKET_NAME" ] || [ -z "$DISTRIBUTION_ID" ] || [ -z "$ADMIN_URL" ] \
    || [ -z "$COGNITO_DOMAIN" ] || [ -z "$COGNITO_CLIENT_ID" ]; then
    echo -e "${RED}Error: could not read admin/Cognito outputs from stack $STACK_NAME.${NC}"
    echo -e "${YELLOW}Run 'cdk deploy' with the latest code, then retry.${NC}"
    exit 1
fi

echo -e "${GREEN}✓ Bucket: $BUCKET_NAME${NC}"
echo -e "${GREEN}✓ Distribution: $DISTRIBUTION_ID${NC}"
echo -e "${GREEN}✓ Admin URL (Cognito redirect): $ADMIN_URL${NC}"

# Real environment variables override .env in Vite, so these get bundled into the build
export VITE_COGNITO_DOMAIN="$COGNITO_DOMAIN"
export VITE_COGNITO_CLIENT_ID="$COGNITO_CLIENT_ID"
export VITE_COGNITO_REDIRECT_URI="$ADMIN_URL"
export VITE_COGNITO_LOGOUT_URI="$ADMIN_URL"
export VITE_COGNITO_IDP_NAME="$COGNITO_IDP_NAME"

echo -e "\n${YELLOW}Building admin frontend...${NC}"
cd "$ADMIN_DIR"
[ -d node_modules ] || pnpm install
pnpm run build

# Fail before uploading if the Cognito values did not get baked into the bundle
if ! grep -rqF "$COGNITO_CLIENT_ID" dist/assets; then
    echo -e "${RED}Error: Cognito client ID not found in the built bundle. Not uploading.${NC}"
    exit 1
fi

echo -e "\n${YELLOW}Uploading to S3...${NC}"
aws s3 sync dist/ "s3://$BUCKET_NAME/" \
    --delete \
    --cache-control "public, max-age=31536000, immutable" \
    --exclude "index.html"
aws s3 cp dist/index.html "s3://$BUCKET_NAME/index.html" \
    --cache-control "no-cache, no-store, must-revalidate" \
    --content-type "text/html"

echo -e "\n${YELLOW}Invalidating CloudFront cache...${NC}"
aws cloudfront create-invalidation --distribution-id "$DISTRIBUTION_ID" --paths "/*" \
    --query "Invalidation.Id" --output text

echo -e "\n${GREEN}Admin UI deployed: $ADMIN_URL${NC}"
