<#
.SYNOPSIS
    Verify Amazon Bedrock model access is enabled for the required providers.

.DESCRIPTION
    Windows-native sibling of scripts/check_bedrock_access.sh. Feature
    parity is enforced (requirements.md NFR-RT-7): same inputs, same exit
    codes, same documentation pointers.

    What "failure" means
      The script exits with code 1 before any AWS resources are touched.
      The reader opens the Amazon Bedrock model-access console, requests access
      for the Anthropic Claude and Amazon Nova families, and re-runs.

    Method
      - Call ``bedrock:ListFoundationModels`` filtered by provider.
      - Catalog presence is necessary but not sufficient; production use
        should additionally issue a tiny InvokeModel probe. That costs a
        few cents per run and is left as a future extension.

    Requirements mapping: 10.2, 12.1, 12.2.

.LINK
    https://console.aws.amazon.com/bedrock/home#/modelaccess
#>

[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"

$BedrockAccessConsole = "https://console.aws.amazon.com/bedrock/home#/modelaccess"

$Region = $env:AWS_REGION
if ([string]::IsNullOrWhiteSpace($Region)) { $Region = $env:AWS_DEFAULT_REGION }
if ([string]::IsNullOrWhiteSpace($Region)) {
    if (Get-Command aws -ErrorAction SilentlyContinue) {
        try { $Region = (& aws configure get region 2>$null).Trim() } catch { $Region = "" }
    }
}
if ([string]::IsNullOrWhiteSpace($Region)) {
    Write-Error "No AWS region configured. Run scripts/check_region.ps1 first."
    exit 1
}

if (-not (Get-Command aws -ErrorAction SilentlyContinue)) {
    Write-Error "The 'aws' CLI is required but was not found on PATH."
    exit 1
}

$fail = $false
foreach ($provider in @("anthropic", "amazon")) {
    $models = ""
    try {
        $models = & aws bedrock list-foundation-models `
            --region $Region `
            --by-provider $provider `
            --query "modelSummaries[].modelId" `
            --output text 2>$null
    } catch {
        $models = ""
    }

    if ([string]::IsNullOrWhiteSpace($models) -or $models -eq "None") {
        Write-Warning "No Amazon Bedrock models returned for provider '$provider' in $Region."
        Write-Warning "Enable access for the $provider model family here:"
        Write-Warning "  $BedrockAccessConsole"
        $fail = $true
    } else {
        $preview = ($models -split "\s+" | Where-Object { $_ } | Select-Object -First 3) -join " "
        Write-Host "OK: $provider models available in ${Region} (first 3): $preview"
    }
}

if ($fail) {
    Write-Error @"
Amazon Bedrock model access is not fully enabled.
Request access for Anthropic Claude and Amazon Nova families:
  $BedrockAccessConsole
"@
    exit 1
}

Write-Host "OK: Amazon Bedrock model access enabled for required providers."
