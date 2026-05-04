<#
.SYNOPSIS
    Informational sweep run after cleanup.ps1 to list any resources that
    *look* like they belong to the M&A Due Diligence sample.

.DESCRIPTION
    Windows-native sibling of scripts/verify_cleanup.sh. Feature parity
    is enforced by Requirement NFR-RT-7 / 11.7.

    This script is informational
      - A handful of sample resources are created imperatively via
        Custom Resources (ECR images, KB data sources, Memory records).
        CDK destroys them on teardown, but an interrupted earlier run
        can leave orphans.
      - AgentCore control-plane APIs are still evolving; some list
        calls may 404 or AccessDenied depending on region/account
        posture. This script treats those as "API unavailable" rather
        than a failure.

    Exit code
      Always 0. The caller (cleanup.ps1) uses this output as guidance,
      not as a pass/fail gate.

.LINK
    Requirements mapping: 8.4, 8.5.
#>

[CmdletBinding()]
param()

# NOTE: we deliberately keep $ErrorActionPreference at its default. This
# script expects individual AWS CLI calls to fail in benign ways (API
# not available, empty responses) and we want the sweep to continue
# regardless.

$Region = $env:AWS_REGION
if ([string]::IsNullOrWhiteSpace($Region)) { $Region = $env:AWS_DEFAULT_REGION }
if ([string]::IsNullOrWhiteSpace($Region)) {
    if (Get-Command aws -ErrorAction SilentlyContinue) {
        try { $Region = (& aws configure get region 2>$null).Trim() } catch { $Region = "" }
    }
}

if ([string]::IsNullOrWhiteSpace($Region)) {
    Write-Host "verify_cleanup: no AWS region configured; skipping sweep."
    exit 0
}

if (-not (Get-Command aws -ErrorAction SilentlyContinue)) {
    Write-Host "verify_cleanup: 'aws' CLI not found on PATH; skipping sweep."
    exit 0
}

function Write-Section {
    param([string]$Title)
    Write-Host ""
    Write-Host "--- $Title ---"
}

function Invoke-AwsListing {
    <#
        Run an AWS CLI invocation and print its output. If the tool
        errors out (e.g. API not yet available in the region) print a
        short "unavailable" note instead of the raw stderr dump.
    #>
    param(
        [Parameter(Mandatory = $true)] [string]$Label,
        [Parameter(Mandatory = $true)] [string[]]$Arguments
    )
    $output = & aws @Arguments 2>&1
    $exit = $LASTEXITCODE
    if ($exit -ne 0) {
        Write-Host "  (skipped: $Label API unavailable in $Region)"
        return
    }
    $text = ($output | Out-String).Trim()
    if ([string]::IsNullOrWhiteSpace($text) -or $text -eq "None" -or $text -eq "[]") {
        Write-Host "  (none)"
    } else {
        $text -split "`r?`n" | ForEach-Object { Write-Host "  $_" }
    }
}

Write-Host "verify_cleanup: scanning region $Region for 'mna-' prefixed resources."

Write-Section "S3 buckets matching 'mna-'"
Invoke-AwsListing -Label "s3" -Arguments @(
    "s3api", "list-buckets",
    "--query", "Buckets[?starts_with(Name, 'mna-')].Name",
    "--output", "text"
)

Write-Section "ECR repositories matching 'mna'"
Invoke-AwsListing -Label "ecr" -Arguments @(
    "ecr", "describe-repositories",
    "--region", $Region,
    "--query", "repositories[?contains(repositoryName, 'mna')].repositoryName",
    "--output", "text"
)

Write-Section "Bedrock Knowledge Bases matching 'mna'"
Invoke-AwsListing -Label "bedrock-agent" -Arguments @(
    "bedrock-agent", "list-knowledge-bases",
    "--region", $Region,
    "--query", "knowledgeBaseSummaries[?contains(name, 'mna')].knowledgeBaseId",
    "--output", "text"
)

Write-Section "AgentCore runtimes"
# The AgentCore control-plane API is still evolving. Treat a missing
# subcommand or AccessDenied as "nothing to verify" rather than a
# failure — this script is purely informational.
Invoke-AwsListing -Label "bedrock-agentcore-control:list-agent-runtimes" -Arguments @(
    "bedrock-agentcore-control", "list-agent-runtimes",
    "--region", $Region,
    "--output", "text"
)

Write-Section "AgentCore memories"
Invoke-AwsListing -Label "bedrock-agentcore-control:list-memories" -Arguments @(
    "bedrock-agentcore-control", "list-memories",
    "--region", $Region,
    "--output", "text"
)

Write-Section "AgentCore gateways"
Invoke-AwsListing -Label "bedrock-agentcore-control:list-gateways" -Arguments @(
    "bedrock-agentcore-control", "list-gateways",
    "--region", $Region,
    "--output", "text"
)

Write-Section "CloudFormation stacks starting with 'Mna'"
Invoke-AwsListing -Label "cloudformation" -Arguments @(
    "cloudformation", "list-stacks",
    "--region", $Region,
    "--stack-status-filter", "CREATE_COMPLETE", "UPDATE_COMPLETE", "DELETE_FAILED", "ROLLBACK_COMPLETE",
    "--query", "StackSummaries[?starts_with(StackName, 'Mna')].StackName",
    "--output", "text"
)

Write-Host ""
Write-Host "verify_cleanup: sweep complete. Any '(none)' section is expected."
Write-Host "               Listed resources above should be removed manually."
exit 0
