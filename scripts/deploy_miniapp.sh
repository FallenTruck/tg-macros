#!/usr/bin/env bash

set -euo pipefail

profile="${AWS_PROFILE:-fitness-dev}"
region="${AWS_REGION:-ap-southeast-1}"
stack_name="${STACK_NAME:-tg-macros-dev}"

aws_args=(--profile "$profile" --region "$region")

bucket_name="$(aws "${aws_args[@]}" cloudformation describe-stacks \
  --stack-name "$stack_name" \
  --query 'Stacks[0].Outputs[?OutputKey==`MiniAppBucketName`].OutputValue | [0]' \
  --output text)"
distribution_id="$(aws "${aws_args[@]}" cloudformation list-stack-resources \
  --stack-name "$stack_name" \
  --query 'StackResourceSummaries[?LogicalResourceId==`MiniAppDistribution`].PhysicalResourceId | [0]' \
  --output text)"
miniapp_url="$(aws "${aws_args[@]}" cloudformation describe-stacks \
  --stack-name "$stack_name" \
  --query 'Stacks[0].Outputs[?OutputKey==`MiniAppUrl`].OutputValue | [0]' \
  --output text)"

if [[ -z "$bucket_name" || "$bucket_name" == "None" ]]; then
  echo "Mini App bucket output was not found for stack $stack_name" >&2
  exit 1
fi
if [[ -z "$distribution_id" || "$distribution_id" == "None" ]]; then
  echo "Mini App distribution was not found for stack $stack_name" >&2
  exit 1
fi
if [[ -z "$miniapp_url" || "$miniapp_url" == "None" ]]; then
  echo "Mini App URL output was not found for stack $stack_name" >&2
  exit 1
fi

echo "Deploying miniapp/ to s3://$bucket_name/ using stack $stack_name"

# Keep the bucket's existing prefixed assets: index.html references these paths
# and the bucket also contains older deployment-managed objects outside miniapp/.
aws s3 sync miniapp/ "s3://$bucket_name/" "${aws_args[@]}" --only-show-errors
aws s3 cp miniapp/app.js "s3://$bucket_name/miniapp/static/app.js" "${aws_args[@]}" --only-show-errors
aws s3 cp miniapp/styles.css "s3://$bucket_name/miniapp/static/styles.css" "${aws_args[@]}" --only-show-errors

invalidation_id="$(aws cloudfront create-invalidation "${aws_args[@]}" \
  --distribution-id "$distribution_id" \
  --paths /index.html /app.js /styles.css /miniapp/static/app.js /miniapp/static/styles.css \
  --query 'Invalidation.Id' \
  --output text)"

echo "Waiting for CloudFront invalidation $invalidation_id"
aws cloudfront wait invalidation-completed "${aws_args[@]}" \
  --distribution-id "$distribution_id" \
  --id "$invalidation_id"

echo "Mini App deployed: $miniapp_url"
echo "CloudFront invalidation completed: $invalidation_id"
