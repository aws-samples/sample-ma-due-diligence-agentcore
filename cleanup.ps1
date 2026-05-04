<#
.SYNOPSIS
    One-command teardown of the M&A Due Diligence AgentCore sample on
    Windows.

.DESCRIPTION
    Windows-native sibling of cleanup.sh. Feature parity is enforced by
    Requirement NFR-RT-7 / 11.7: both scripts accept equivalent flags,
    return the same exit codes on failure, and print the same final
    verification cheat sheet.

    What this script does (in order)
      1. Confirm with the reader before any destructive action. Accepts
         -Force to skip the prompt for CI and scripted use.
      2. `cdk destroy --all --force` — CDK handles the reverse ordering
         (Agent → Gateway → Evaluator → Data → Network) automatically
         from the dependency graph declared in infra/app.py
         (Requirement 8.3).
      3. Run scripts/verify_cleanup.ps1. That script is informational:
         it lists any resources that *look* like they belong to the
         sample and prints commands the reader can copy-paste to remove
         them manually (Requirement 8.5).
      4. Print a copy-paste cheat sheet of manual verification commands.

    Why cleanup remains best-effort
      A handful of resources are created imperatively via Custom
      Resources and *should* delete cleanly, but CloudFormation cannot
      guarantee the deletion of everything (orphaned ECR images, KB
      ingestion artifacts, Memory records). The verify script helps the
      reader confirm a zero-cost end state (Requirement 8.4, 8.5).

    Exit codes
      0  cdk destroy completed (verify script may still print findings,
         which is informational, not a failure)
      1  reader declined the confirmation prompt, or destroy failed

.PARAMETER Force
    Skip the confirmation prompt. Intended for CI and scripted use.

.PARAMETER SkipVerify
    Skip scripts/verify_cleanup.ps1.

.LINK
    Requirements mapping: 8.3, 8.4, 8.5, NFR-RT-7 (feature parity).
#>

[CmdletBinding()]
param(
    [switch]$Force,
    [switch]$SkipVerify
)

$ErrorActionPreference = "Stop"

$RepoRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $RepoRoot

function Write-Step {
    param([string]$Message)
    Write-Host ""
    Write-Host "[cleanup] $Message"
}

function Invoke-Checked {
    param(
        [Parameter(Mandatory = $true)] [string]$Command,
        [Parameter(ValueFromRemainingArguments = $true)] [string[]]$Args
    )
    & $Command @Args
    if ($LASTEXITCODE -ne 0) {
        throw "Command failed with exit code ${LASTEXITCODE}: $Command $($Args -join ' ')"
    }
}

# ---------------------------------------------------------------------------
# Step 1: Confirmation
# ---------------------------------------------------------------------------
if (-not $Force) {
    @"

================================================================================
  M&A Due Diligence sample cleanup
================================================================================

This will permanently destroy all CDK stacks created by this sample:
  - MnaAgentStack       (AgentCore Runtime, Memory, Guardrail, ECR, CodeBuild)
  - MnaGatewayStack     (AgentCore Gateway + market-data Lambda)
  - MnaEvaluatorStack   (citation-check Lambda)
  - MnaDataStack        (Aurora Serverless v2, DynamoDB, S3, Bedrock KB)
  - MnaNetworkStack     (VPC, subnets, endpoints)

All data in S3, DynamoDB, Aurora, and AgentCore Memory will be deleted.

"@ | Write-Host

    $confirm = Read-Host "Type 'yes' to continue, anything else to abort"
    if ($confirm -ne "yes") {
        Write-Host "Aborted."
        exit 1
    }
}

# ---------------------------------------------------------------------------
# Step 2: cdk destroy --all --force
# ---------------------------------------------------------------------------
Write-Step "Step 1/3: cdk destroy --all --force"
# CDK determines the reverse order from stack dependencies declared in
# infra\app.py. --force suppresses the secondary interactive prompt so
# the script remains one-command.
Push-Location (Join-Path $RepoRoot "infra")
try {
    # See deploy.ps1 for why we call 'cdk' directly rather than via
    # 'npx --yes cdk' on Windows.
    Invoke-Checked "cdk" "destroy" "--all" "--force"
} finally {
    Pop-Location
}

# ---------------------------------------------------------------------------
# Step 3: Verification sweep
# ---------------------------------------------------------------------------
if (-not $SkipVerify) {
    Write-Step "Step 2/3: Verification sweep (informational)"
    $VerifyScript = Join-Path $RepoRoot "scripts\verify_cleanup.ps1"
    if (Test-Path $VerifyScript) {
        # verify_cleanup.ps1 always exits 0 (findings are informational)
        # so a failure here would only indicate a real script bug. Even
        # so, we catch exceptions to let the outer script print the
        # manual cheat sheet even if verification blew up.
        try {
            & $VerifyScript
        } catch {
            Write-Warning "verify_cleanup.ps1 raised: $_"
        }
    } else {
        Write-Warning "scripts\verify_cleanup.ps1 not found."
    }
} else {
    Write-Step "Step 2/3: Verification skipped (-SkipVerify)"
}

# ---------------------------------------------------------------------------
# Step 4: Manual verification cheat sheet
# ---------------------------------------------------------------------------
Write-Step "Step 3/3: Cleanup complete"

$Region = $env:AWS_REGION
if ([string]::IsNullOrWhiteSpace($Region)) { $Region = $env:AWS_DEFAULT_REGION }
if ([string]::IsNullOrWhiteSpace($Region)) {
    if (Get-Command aws -ErrorAction SilentlyContinue) {
        try { $Region = (& aws configure get region 2>$null).Trim() } catch { $Region = "" }
    }
}
if ([string]::IsNullOrWhiteSpace($Region)) { $Region = "<your-region>" }

@"

Run the following to double-check that no billable resources remain
(Requirement 8.5). All commands should return empty lists.

  # S3 buckets created by the sample:
  aws s3 ls --region $Region | Select-String 'mna-'

  # ECR repositories:
  aws ecr describe-repositories --region $Region ``
    --query "repositories[?contains(repositoryName, 'mna')].repositoryName" ``
    --output text

  # Bedrock Knowledge Bases:
  aws bedrock-agent list-knowledge-bases --region $Region ``
    --query "knowledgeBaseSummaries[?contains(name, 'mna')].{id:knowledgeBaseId,name:name}" ``
    --output table

  # AgentCore Runtimes, Memories, Gateways (if API available):
  aws bedrock-agentcore-control list-agent-runtimes --region $Region
  aws bedrock-agentcore-control list-memories       --region $Region
  aws bedrock-agentcore-control list-gateways       --region $Region

  # CloudFormation: every Mna* stack should be gone.
  aws cloudformation list-stacks --region $Region ``
    --stack-status-filter CREATE_COMPLETE UPDATE_COMPLETE DELETE_FAILED ``
    --query "StackSummaries[?starts_with(StackName, 'Mna')].StackName" ``
    --output text

If any of the above returns a non-empty result, open the AWS console
for that service and delete the listed resources. Orphaned ECR images
and KB data sources occasionally survive a CDK destroy when a previous
deploy exited mid-run.
"@ | Write-Host
