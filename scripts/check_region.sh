#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# check_region.sh
#
# Purpose
#   Verify that the AWS region the reader is about to deploy into is one
#   where Amazon Amazon Bedrock AgentCore is generally available.
#
# What "failure" means
#   The script exits with status 1 and prints a pointer to the AgentCore
#   regions documentation. Nothing has been deployed yet, so there is no
#   rollback: the reader simply re-runs with a supported region.
#
# Supported regions (v1)
#   us-east-1, us-west-2
#
# The full list is expected to grow. The README (task 39) expands on this
# list and links to the authoritative AgentCore regions documentation.
#
# Requirements mapping: 10.1, 10.2, 10.3.
# ---------------------------------------------------------------------------

set -euo pipefail

# Keep this list in sync with cdk.json ``mnaAgentCoreSupportedRegions``.
SUPPORTED_REGIONS=(
  "us-east-1"
  "us-west-2"
)

AGENTCORE_REGIONS_DOC="https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/agentcore-regions.html"

# Resolve the target region from the same sources the AWS CLI uses:
#   1. AWS_REGION (explicit override)
#   2. AWS_DEFAULT_REGION (legacy env var)
#   3. The default profile's configured region
REGION="${AWS_REGION:-${AWS_DEFAULT_REGION:-}}"
if [ -z "$REGION" ]; then
  if command -v aws >/dev/null 2>&1; then
    REGION="$(aws configure get region 2>/dev/null || true)"
  fi
fi

if [ -z "$REGION" ]; then
  echo "ERROR: No AWS region configured." >&2
  echo "       Set AWS_REGION, AWS_DEFAULT_REGION, or run 'aws configure'." >&2
  echo "       Supported regions: ${SUPPORTED_REGIONS[*]}" >&2
  echo "       Docs: $AGENTCORE_REGIONS_DOC" >&2
  exit 1
fi

for supported in "${SUPPORTED_REGIONS[@]}"; do
  if [ "$REGION" = "$supported" ]; then
    echo "OK: Region '$REGION' is a supported Amazon Bedrock AgentCore region."
    exit 0
  fi
done

echo "ERROR: Region '$REGION' is not a supported Amazon Bedrock AgentCore region." >&2
echo "       Supported regions: ${SUPPORTED_REGIONS[*]}" >&2
echo "       Set AWS_REGION to one of the supported values and re-run." >&2
echo "       For the authoritative list of AgentCore regions, see:" >&2
echo "         $AGENTCORE_REGIONS_DOC" >&2
exit 1
