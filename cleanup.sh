#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# cleanup.sh
#
# Purpose
#   One-command teardown of the M&A Due Diligence AgentCore sample on
#   macOS/Linux. POSIX-bash sibling of cleanup.ps1 (feature parity
#   enforced by Requirement NFR-RT-7 / 11.7).
#
# What this script does (in order)
#   1. Confirm with the reader before any destructive action. Accepts
#      --yes / --force / -y to skip the prompt for CI and scripted use.
#   2. `cdk destroy --all --force` — CDK handles the reverse ordering
#      (Agent → Gateway → Evaluator → Data → Network) automatically from
#      the dependency graph declared in infra/app.py (Requirement 8.3).
#   3. Run scripts/verify_cleanup.sh. That script is informational: it
#      lists any resources that *look* like they belong to the sample
#      and prints commands the reader can copy-paste to remove them
#      manually (Requirement 8.5).
#   4. Print a copy-paste cheat sheet of manual verification commands.
#
# Why cleanup remains best-effort
#   A handful of resources are created imperatively via Custom Resources
#   and *should* delete cleanly, but CloudFormation cannot guarantee the
#   deletion of everything (orphaned ECR images, KB ingestion artifacts,
#   Memory records). The verify_cleanup script helps the reader confirm
#   a zero-cost end state (Requirement 8.4, 8.5).
#
# Exit codes
#   0  cdk destroy completed (verify script may still print findings,
#      which is informational, not a failure)
#   1  reader declined the confirmation prompt, or destroy failed
#
# Optional flags
#   -y / --yes / --force  Skip the confirmation prompt.
#   --skip-verify         Skip scripts/verify_cleanup.sh.
#
# Requirements mapping: 8.3, 8.4, 8.5, NFR-RT-7 (feature parity).
# ---------------------------------------------------------------------------

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$REPO_ROOT"

FORCE=0
SKIP_VERIFY=0

for arg in "$@"; do
  case "$arg" in
    -y|--yes|--force) FORCE=1 ;;
    --skip-verify)    SKIP_VERIFY=1 ;;
    -h|--help)
      sed -n '2,40p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
      exit 0
      ;;
    *)
      echo "ERROR: Unknown argument '$arg'. Use --help for usage." >&2
      exit 1
      ;;
  esac
done

log() { printf '\n[cleanup] %s\n' "$*"; }

# ---------------------------------------------------------------------------
# Step 1: Confirmation
# ---------------------------------------------------------------------------
if [ "$FORCE" -eq 0 ]; then
  cat <<'EOF'

================================================================================
  M&A Due Diligence sample cleanup
================================================================================

This will permanently destroy all CDK stacks created by this sample:
  - MnaAgentStack       (AgentCore Runtime, Memory, Guardrail, Amazon ECR, AWS CodeBuild)
  - MnaGatewayStack     (AgentCore Gateway + market-data Lambda)
  - MnaEvaluatorStack   (citation-check Lambda)
  - MnaDataStack        (Aurora Serverless v2, DynamoDB, S3, Bedrock KB)
  - MnaNetworkStack     (VPC, subnets, endpoints)

All data in S3, DynamoDB, Aurora, and AgentCore Memory will be deleted.

EOF
  # -r disables backslash escapes; prompt stays visible on one line.
  read -r -p "Enter 'yes' to continue, anything else to cancel: " CONFIRM
  if [ "$CONFIRM" != "yes" ]; then
    echo "Aborted."
    exit 1
  fi
else
  # --force / -y skips the interactive prompt but still surfaces a one-line
  # data-loss notice so CI logs and automated runs make the destructive
  # action visible to operators.
  log "WARNING: --force enabled; deleting all data in S3, DynamoDB, Aurora, and AgentCore Memory."
fi

# ---------------------------------------------------------------------------
# Step 2: cdk destroy --all --force
# ---------------------------------------------------------------------------
log "Step 1/3: cdk destroy --all --force"
# CDK determines the reverse order from stack dependencies declared in
# infra/app.py. --force suppresses the secondary interactive prompt so
# the script remains one-command.
(
  cd "$REPO_ROOT/infra"
  cdk destroy --all --force
)

# ---------------------------------------------------------------------------
# Step 3: Verification sweep
# ---------------------------------------------------------------------------
if [ "$SKIP_VERIFY" -eq 0 ]; then
  log "Step 2/3: Verification sweep (informational)"
  if [ -x "$REPO_ROOT/scripts/verify_cleanup.sh" ]; then
    # verify_cleanup.sh always exits 0 (findings are informational) so a
    # set -e failure here would only indicate a real script bug.
    bash "$REPO_ROOT/scripts/verify_cleanup.sh" || true
  else
    echo "Warning: scripts/verify_cleanup.sh not found or not executable." >&2
  fi
else
  log "Step 2/3: Verification skipped (--skip-verify)"
fi

# ---------------------------------------------------------------------------
# Step 4: Manual verification cheat sheet
# ---------------------------------------------------------------------------
log "Step 3/3: Cleanup complete"

REGION="${AWS_REGION:-${AWS_DEFAULT_REGION:-}}"
if [ -z "$REGION" ] && command -v aws >/dev/null 2>&1; then
  REGION="$(aws configure get region 2>/dev/null || true)"
fi
REGION="${REGION:-<your-region>}"

cat <<EOF

Run the following to double-check that no billable resources remain
(Requirement 8.5). All commands should return empty lists.

  # S3 buckets created by the sample:
  aws s3 ls --region $REGION | grep -i 'mna-'

  # ECR repositories:
  aws ecr describe-repositories --region $REGION \\
    --query "repositories[?contains(repositoryName, 'mna')].repositoryName" \\
    --output text

  # Amazon Bedrock Knowledge Bases:
  aws bedrock-agent list-knowledge-bases --region $REGION \\
    --query "knowledgeBaseSummaries[?contains(name, 'mna')].{id:knowledgeBaseId,name:name}" \\
    --output table

  # AgentCore Runtimes, Memories, Gateways (if API available):
  aws bedrock-agentcore-control list-agent-runtimes --region $REGION 2>/dev/null || true
  aws bedrock-agentcore-control list-memories       --region $REGION 2>/dev/null || true
  aws bedrock-agentcore-control list-gateways       --region $REGION 2>/dev/null || true

  # CloudFormation: every Mna* stack should be gone.
  aws cloudformation list-stacks --region $REGION \\
    --stack-status-filter CREATE_COMPLETE UPDATE_COMPLETE DELETE_FAILED \\
    --query "StackSummaries[?starts_with(StackName, 'Mna')].StackName" \\
    --output text

If any of the above returns a non-empty result, open the AWS console
for that service and delete the listed resources. Orphaned ECR images
and KB data sources occasionally survive a CDK destroy when a previous
deploy exited mid-run.
EOF
