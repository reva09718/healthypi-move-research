#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
REGION=ap-southeast-1
STACK=healthypi-mobile
read -r -p 'Comma-separated allowed project emails (your email and teammates): ' EMAILS
if [[ -z "$EMAILS" ]]; then echo 'No emails entered; nothing deployed.'; exit 1; fi
python3 build_template.py
aws cloudformation deploy --region "$REGION" --stack-name "$STACK" \
  --template-file template.generated.json --capabilities CAPABILITY_IAM \
  --parameter-overrides "AllowedSignupEmails=$EMAILS"
aws cloudformation describe-stacks --region "$REGION" --stack-name "$STACK" \
  --query 'Stacks[0].Outputs[?OutputKey==`AppConfig`].OutputValue' --output text
echo 'Paste that JSON into the app: Settings > Research recordings / AWS > Account & AWS setup.'
