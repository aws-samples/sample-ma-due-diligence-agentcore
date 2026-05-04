#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# check_bedrock_access.sh
#
# Purpose
#   Verify that the caller's AWS account has Bedrock model access enabled
#   for the Anthropic Claude and Amazon Nova model families that the
#   sample depends on. Bedrock model access is an opt-in per region.
#
# What "failure" means
#   The script exits with status 1 and prints a link to the Bedrock model
#   access console. No resources are created yet, so the reader just
#   requests access (one click per family) and re-runs.
#
# Method
#   - Call ``bedrock:ListFoundationModels`` filtered by provider.
#   - The API returns provider catalog entries even when the caller has
#     not yet requested model access, so catalog presence is necessary
#     but not sufficient. Production hardening would additionally issue a
#     tiny ``InvokeModel`` call; that costs a few cents per run which is
#     acceptable but left as a future extension.
#
# Requirements mapping: 10.2 (fail-fast on unsupported environments),
#                      12.1, 12.2 (Anthropic Claude + Amazon Nova defaults).
# ---------------------------------------------------------------------------

set -euo pipefail

BEDROCK_ACCESS_CONSOLE="https://console.aws.amazon.com/bedrock/home#/modelaccess"

# Resolve region the same way check_region.sh does.
REGION="${AWS_REGION:-${AWS_DEFAULT_REGION:-}}"
if [ -z "$REGION" ] && command -v aws >/dev/null 2>&1; then
  REGION="$(aws configure get region 2>/dev/null || true)"
fi

if [ -z "$REGION" ]; then
  echo "ERROR: No AWS region configured. Run scripts/check_region.sh first." >&2
  exit 1
fi

if ! command -v aws >/dev/null 2>&1; then
  echo "ERROR: The 'aws' CLI is required but was not found on PATH." >&2
  exit 1
fi

fail=0
for provider in anthropic amazon; do
  # ``--query`` uses JMESPath; a missing catalog entry yields an empty
  # string, which the case below treats as a failure.
  models="$(aws bedrock list-foundation-models \
    --region "$REGION" \
    --by-provider "$provider" \
    --query "modelSummaries[].modelId" \
    --output text 2>/dev/null || true)"

  if [ -z "$models" ] || [ "$models" = "None" ]; then
    echo "ERROR: No Bedrock models returned for provider '$provider' in $REGION." >&2
    echo "       Enable access for the $provider model family here:" >&2
    echo "         $BEDROCK_ACCESS_CONSOLE" >&2
    fail=1
  else
    # Trim to a short preview for human readability.
    preview="$(echo "$models" | tr '\t' '\n' | head -n 3 | tr '\n' ' ')"
    echo "OK: $provider models available in $REGION (first 3): $preview"
  fi
done

if [ "$fail" -ne 0 ]; then
  echo "ERROR: Bedrock model access is not fully enabled." >&2
  echo "       Request access for Anthropic Claude and Amazon Nova families:" >&2
  echo "         $BEDROCK_ACCESS_CONSOLE" >&2
  exit 1
fi

echo "OK: Bedrock model access enabled for required providers."
