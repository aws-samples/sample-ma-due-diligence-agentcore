<#
.SYNOPSIS
    Verify the target AWS region supports AWS Bedrock AgentCore.

.DESCRIPTION
    Windows-native sibling of scripts/check_region.sh. Feature parity is a
    hard requirement (see requirements.md NFR-RT-7): both scripts accept
    the same inputs, return the same non-zero exit code on failure, and
    point at the same documentation URL.

    What "failure" means
      The script exits with code 1 before any AWS resources are touched.
      The reader simply re-runs deploy in a supported region.

    Supported regions (v1)
      us-east-1, us-west-2

    Requirements mapping: 10.1, 10.2, 10.3.

.LINK
    https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/agentcore-regions.html
#>

[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"

# Keep this list in sync with cdk.json mnaAgentCoreSupportedRegions.
$SupportedRegions = @("us-east-1", "us-west-2")
$AgentCoreRegionsDoc = "https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/agentcore-regions.html"

# Resolve the target region from the same sources the AWS CLI uses.
$Region = $env:AWS_REGION
if ([string]::IsNullOrWhiteSpace($Region)) {
    $Region = $env:AWS_DEFAULT_REGION
}
if ([string]::IsNullOrWhiteSpace($Region)) {
    if (Get-Command aws -ErrorAction SilentlyContinue) {
        try {
            $Region = (& aws configure get region 2>$null).Trim()
        } catch {
            $Region = ""
        }
    }
}

if ([string]::IsNullOrWhiteSpace($Region)) {
    Write-Error @"
No AWS region configured.
Set AWS_REGION, AWS_DEFAULT_REGION, or run 'aws configure'.
Supported regions: $($SupportedRegions -join ', ')
Docs: $AgentCoreRegionsDoc
"@
    exit 1
}

if ($SupportedRegions -contains $Region) {
    Write-Host "OK: Region '$Region' is a supported Bedrock AgentCore region."
    exit 0
}

Write-Error @"
Region '$Region' is not a supported Bedrock AgentCore region.
Supported regions: $($SupportedRegions -join ', ')
Set AWS_REGION to one of the supported values and re-run.
For the authoritative list of AgentCore regions, see:
  $AgentCoreRegionsDoc
"@
exit 1
