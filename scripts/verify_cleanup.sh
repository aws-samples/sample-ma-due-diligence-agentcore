#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# verify_cleanup.sh
#
# Purpose
#   Informational sweep run after ``cleanup.sh`` (Requirement 8.5). Lists
#   any AWS resources that *look* like they belong to the M&A Due
#   Diligence sample so the reader can confirm CDK destroyed everything
#   and manually remove any stragglers.
#
# Why this script is informational
#   - A handful of sample resources are created imperatively via Custom
#     Resources (ECR images, KB data sources, Memory records). CDK
#     destroys them on teardown, but an interrupted earlier run can
#     leave orphans.
#   - AgentCore control-plane APIs are still evolving; some list calls
#     may 404 or AccessDenied depending on region/account posture. This
#     script treats those as "API unavailable" rather than a failure.
#
# Exit code
#   Always 0. The caller (cleanup.sh) uses this output as guidance, not
#   as a pass/fail gate.
#
# Requirements mapping: 8.4, 8.5.
# ---------------------------------------------------------------------------

set -uo pipefail

# NOTE: intentionally not using `set -e`. Individual AWS CLI calls are
# expected to fail in benign ways (API not available, empty responses)
# and we want the sweep to continue regardless.

REGION="${AWS_REGION:-${AWS_DEFAULT_REGION:-}}"
if [ -z "$REGION" ] && command -v aws >/dev/null 2>&1; then
  REGION="$(aws configure get region 2>/dev/null || true)"
fi

if [ -z "$REGION" ]; then
  echo "verify_cleanup: no AWS region configured; skipping sweep."
  exit 0
fi

if ! command -v aws >/dev/null 2>&1; then
  echo "verify_cleanup: 'aws' CLI not found on PATH; skipping sweep."
  exit 0
fi

print_section() {
  echo ""
  echo "--- $1 ---"
}

run_or_note() {
  # Run an AWS CLI command; if the tool errors out (e.g. API not yet
  # available in the region), print a short "unavailable" note instead
  # of the full stderr dump. Keeps the cleanup output readable.
  local label="$1"
  shift
  local out
  if ! out="$("$@" 2>&1)"; then
    echo "  (skipped: $label API unavailable in $REGION)"
    return 0
  fi
  if [ -z "$out" ] || [ "$out" = "None" ] || [ "$out" = "[]" ]; then
    echo "  (none)"
  else
    # Indent each line for readability.
    printf '%s\n' "$out" | sed 's/^/  /'
  fi
}

echo "verify_cleanup: scanning region $REGION for 'mna-' prefixed resources."

print_section "S3 buckets matching 'mna-'"
run_or_note "s3" aws s3api list-buckets \
  --query "Buckets[?starts_with(Name, 'mna-')].Name" \
  --output text

print_section "ECR repositories matching 'mna'"
run_or_note "ecr" aws ecr describe-repositories \
  --region "$REGION" \
  --query "repositories[?contains(repositoryName, 'mna')].repositoryName" \
  --output text

print_section "Bedrock Knowledge Bases matching 'mna'"
run_or_note "bedrock-agent" aws bedrock-agent list-knowledge-bases \
  --region "$REGION" \
  --query "knowledgeBaseSummaries[?contains(name, 'mna')].knowledgeBaseId" \
  --output text

print_section "AgentCore runtimes"
# The AgentCore control-plane API is still evolving. Treat a missing
# subcommand or AccessDenied as "nothing to verify" rather than a
# failure — this script is purely informational.
run_or_note "bedrock-agentcore-control:list-agent-runtimes" \
  aws bedrock-agentcore-control list-agent-runtimes \
  --region "$REGION" \
  --output text

print_section "AgentCore memories"
run_or_note "bedrock-agentcore-control:list-memories" \
  aws bedrock-agentcore-control list-memories \
  --region "$REGION" \
  --output text

print_section "AgentCore gateways"
run_or_note "bedrock-agentcore-control:list-gateways" \
  aws bedrock-agentcore-control list-gateways \
  --region "$REGION" \
  --output text

print_section "CloudFormation stacks starting with 'Mna'"
run_or_note "cloudformation" aws cloudformation list-stacks \
  --region "$REGION" \
  --stack-status-filter CREATE_COMPLETE UPDATE_COMPLETE DELETE_FAILED ROLLBACK_COMPLETE \
  --query "StackSummaries[?starts_with(StackName, 'Mna')].StackName" \
  --output text

echo ""
echo "verify_cleanup: sweep complete. Any '(none)' section is expected."
echo "               Listed resources above should be removed manually."
exit 0
