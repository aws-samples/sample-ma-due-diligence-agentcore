<#
.SYNOPSIS
    One-command end-to-end deployment of the M&A Due Diligence AgentCore
    sample on Windows.

.DESCRIPTION
    Windows-native sibling of deploy.sh. Feature parity is enforced by
    Requirement NFR-RT-7 / 11.7: both scripts accept equivalent flags,
    return the same exit codes on failure, and print the same final
    next-steps message.

    What this script does (in order)
      1. Preflight: region check. Exits non-zero with an actionable
         error message, so deployment fails fast before any billable
         resource is created (Requirement 10.2).
      2. Set up a local Python virtual environment under `.venv\` and
         install pinned dependencies from requirements.txt. Keeps the
         reader's global Python clean (design §"Virtual environment").
      3. `cdk bootstrap` — idempotent. Skipped on accounts where the
         bootstrap stack is already present.
      4. `cdk deploy --all` — AWS CDK resolves the Network → Data →
         Evaluator → Gateway → Agent dependency order from the stack
         graph declared in infra/app.py (Requirement 8.1, 8.2).
      5. Seed synthetic data via `python data/generate.py --seed-all`.
         Resource identifiers are resolved from SSM inside generate.py
         so no extra arguments are needed here.
      6. Print copy-paste instructions to open the walkthrough notebook.

    Exit codes
      0  success
      1  preflight or deployment failure (terminates at the failing step)

.PARAMETER SkipPreflight
    Skip scripts/check_region.ps1. Useful for re-runs where the reader
    already confirmed the environment.

.PARAMETER SkipSeed
    Skip `data/generate.py --seed-all`. Useful if the reader plans to run
    the generator manually with custom overrides.

.PARAMETER SkipVenv
    Use the active Python instead of creating `.venv`. Recommended only
    inside CI where the runner image already has the pinned requirements.

.PARAMETER SkipSmoke
    Skip tests/smoke_test.py. Useful when deploying into a preview
    region where the smoke-test assertions may not yet hold.

.LINK
    Requirements mapping: 8.1, 8.2, NFR-RT-6, NFR-RT-7 (feature parity).
#>

[CmdletBinding()]
param(
    [switch]$SkipPreflight,
    [switch]$SkipSeed,
    [switch]$SkipVenv,
    [switch]$SkipSmoke
)

$ErrorActionPreference = "Stop"

# Resolve the repo root so the script works regardless of PWD.
$RepoRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $RepoRoot

function Write-Step {
    param([string]$Message)
    Write-Host ""
    Write-Host "[deploy] $Message"
}

function Invoke-Checked {
    <#
        Run a native command and fail the script if its exit code is
        non-zero. PowerShell doesn't set $ErrorActionPreference on
        external processes, so we check $LASTEXITCODE explicitly.
    #>
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
# Step 1: Preflight
# ---------------------------------------------------------------------------
if (-not $SkipPreflight) {
    Write-Step "Step 1/7: Preflight checks (region)"
    & "$RepoRoot\scripts\check_region.ps1"
    if ($LASTEXITCODE -ne 0) { throw "check_region.ps1 failed." }
} else {
    Write-Step "Step 1/7: Preflight checks skipped (-SkipPreflight)"
}

# ---------------------------------------------------------------------------
# Step 2: Virtual environment + dependencies
# ---------------------------------------------------------------------------
if (-not $SkipVenv) {
    Write-Step "Step 2/7: Virtual environment and Python dependencies"
    $VenvDir = Join-Path $RepoRoot ".venv"
    if (-not (Test-Path $VenvDir)) {
        Write-Host "Creating .venv"
        # Prefer py launcher -3.11; fall back to 'python' so the script
        # still works on hosts without the versioned launcher.
        if (Get-Command py -ErrorAction SilentlyContinue) {
            Invoke-Checked "py" "-3.11" "-m" "venv" $VenvDir
        } elseif (Get-Command python -ErrorAction SilentlyContinue) {
            Invoke-Checked "python" "-m" "venv" $VenvDir
        } else {
            throw "python not found on PATH. Install Python 3.11+ before running deploy.ps1."
        }
    } else {
        Write-Host ".venv already present — reusing"
    }

    # Activate the venv for the remainder of the script. The activation
    # script updates $env:PATH for the current session so subsequent
    # 'python' and 'pip' calls resolve to the venv.
    $Activate = Join-Path $VenvDir "Scripts\Activate.ps1"
    if (-not (Test-Path $Activate)) {
        throw "Activation script not found at $Activate. The venv may be corrupt; delete .venv and re-run."
    }
    . $Activate

    Write-Host "Upgrading pip and installing pinned requirements.txt"
    Invoke-Checked "python" "-m" "pip" "install" "--quiet" "--upgrade" "pip"
    Invoke-Checked "python" "-m" "pip" "install" "--quiet" "-r" (Join-Path $RepoRoot "requirements.txt")

    # Install this project in editable mode so the `mna` package (used by
    # data/generate.py, tests/smoke_test.py, and the notebook) and the
    # `mna` console-script entry point (used in Step 2 of the walkthrough)
    # are both available. Without this, `mna invoke ...` is not found on
    # PATH and `data/generate.py --seed-all` fails with
    # "ModuleNotFoundError: No module named 'mna'".
    Write-Host "Installing project in editable mode (pip install -e .)"
    Invoke-Checked "python" "-m" "pip" "install" "--quiet" "-e" $RepoRoot

    # --------------------------------------------------------------
    # Vendor boto3 into lambda/_vendor so every CR Lambda ships the
    # same SDK version we tested locally. The Lambda managed runtime
    # bundles an older boto3 whose service models can lag behind the
    # current services (notably Amazon Bedrock AgentCore). Shipping our own
    # copy sidesteps that skew.
    # --------------------------------------------------------------
    $VendorDir = Join-Path $RepoRoot "lambda\_vendor"
    $LambdaReq = Join-Path $RepoRoot "lambda\requirements.txt"
    $VendorStamp = Join-Path $VendorDir ".pinned-from"
    $ExistingStamp = if (Test-Path $VendorStamp) { (Get-Content $VendorStamp -Raw).Trim() } else { "" }
    $DesiredStamp = (Get-Content $LambdaReq -Raw).Trim()
    if ($ExistingStamp -ne $DesiredStamp) {
        Write-Host "Installing lambda/requirements.txt into lambda/_vendor"
        if (Test-Path $VendorDir) { Remove-Item -Recurse -Force $VendorDir }
        New-Item -ItemType Directory -Path $VendorDir | Out-Null
        Invoke-Checked "python" "-m" "pip" "install" "--quiet" "--no-compile" "-r" $LambdaReq "-t" $VendorDir
        Set-Content -Path $VendorStamp -Value $DesiredStamp -NoNewline
    } else {
        Write-Host "lambda/_vendor already matches lambda/requirements.txt — reusing"
    }
} else {
    Write-Step "Step 2/7: Virtual environment skipped (-SkipVenv)"
}

# ---------------------------------------------------------------------------
# Step 3: cdk bootstrap (idempotent)
# ---------------------------------------------------------------------------
Write-Step "Step 3/7: cdk bootstrap (idempotent)"

# Resolve the target region from the CLI/env so the CDKToolkit stack
# probe queries the right place. check_region.ps1 already validated the
# value if preflight was run.
$Region = $env:AWS_REGION
if ([string]::IsNullOrWhiteSpace($Region)) { $Region = $env:AWS_DEFAULT_REGION }
if ([string]::IsNullOrWhiteSpace($Region)) {
    if (Get-Command aws -ErrorAction SilentlyContinue) {
        try { $Region = (& aws configure get region 2>$null).Trim() } catch { $Region = "" }
    }
}
if ([string]::IsNullOrWhiteSpace($Region)) {
    throw "No AWS region configured; cannot run cdk bootstrap."
}

# Probe for the CDKToolkit CloudFormation stack. Redirect stderr so the
# "does not exist" message doesn't look like a script failure.
$null = & aws cloudformation describe-stacks `
    --region $Region `
    --stack-name CDKToolkit `
    --query 'Stacks[0].StackStatus' `
    --output text 2>$null
if ($LASTEXITCODE -eq 0) {
    Write-Host "CDKToolkit stack already bootstrapped in $Region — skipping"
} else {
    Push-Location (Join-Path $RepoRoot "infra")
    try {
        # Call the globally installed 'cdk' (from 'npm install -g
        # aws-cdk') directly. Going through 'npx --yes cdk' is unsafe on
        # Windows because npx.ps1 re-parses its arguments through the
        # PowerShell binder, which mis-binds flags like '--yes' when
        # the function caller uses @Args splatting.
        Invoke-Checked "cdk" "bootstrap"
    } finally {
        Pop-Location
    }
}

# ---------------------------------------------------------------------------
# Step 4: cdk deploy --all
# ---------------------------------------------------------------------------
Write-Step "Step 4/7: cdk deploy --all (Network → Data → Evaluator → Gateway → Agent)"
Push-Location (Join-Path $RepoRoot "infra")
try {
    # --require-approval never suppresses the interactive IAM prompt so
    # the script stays one-command. CDK still prints the change set.
    # See the bootstrap step above for why we bypass 'npx'.
    Invoke-Checked "cdk" "deploy" "--all" "--require-approval" "never"
} finally {
    Pop-Location
}

# ---------------------------------------------------------------------------
# Step 5: Seed synthetic data
# ---------------------------------------------------------------------------
if (-not $SkipSeed) {
    Write-Step "Step 5/7: Seeding synthetic data (data/generate.py --seed-all)"
    Invoke-Checked "python" (Join-Path $RepoRoot "data\generate.py") "--seed-all"
} else {
    Write-Step "Step 5/7: Data seeding skipped (-SkipSeed)"
    Write-Host "Run manually later with:  python data\generate.py --seed-all"
}

# ---------------------------------------------------------------------------
# Step 6: Post-deploy smoke test
# ---------------------------------------------------------------------------
# Task 38 designates tests/smoke_test.py as the final verification
# step of the deploy script. It exercises each specialist with its
# prompts.md prompt, asserts the evaluator returns a pass/fail for
# each, and confirms at least one invocation's X-Ray trace includes
# a Gateway hop (Requirement 15.3, Sample-level AC 2-4).
if (-not $SkipSmoke) {
    Write-Step "Step 6/7: Running post-deploy smoke test"
    # Don't stop the script on a smoke-test failure — the stack is
    # deployed, the reader may want to inspect it manually. Print a
    # clear warning instead and continue to the next-steps message.
    & python -m pytest (Join-Path $RepoRoot "tests\smoke_test.py") -m smoke --no-header -ra
    if ($LASTEXITCODE -eq 0) {
        Write-Host "Smoke test PASSED"
    } else {
        Write-Host "Smoke test FAILED -- inspect the output above. The stack is still deployed."
        Write-Host "You can re-run the smoke test with:  python -m pytest tests/smoke_test.py -m smoke"
    }
} else {
    Write-Step "Step 6/7: Smoke test skipped (-SkipSmoke)"
}

# ---------------------------------------------------------------------------
# Step 7: Next-steps message
# ---------------------------------------------------------------------------
Write-Step "Step 7/7: Deployment complete"
@"

Next steps:
  1. Open the walkthrough notebook:
       jupyter lab notebooks\walkthrough.ipynb
     (or "jupyter notebook notebooks\walkthrough.ipynb")

  2. Alternatively, invoke an agent from the CLI:
       python -m cli.invoke list-agents
       python -m cli.invoke invoke supervisor "Screen mid-market logistics targets."

  3. Tear down all billable resources when you are done:
       .\cleanup.ps1

Cost reminder: leaving the stack deployed continues to accrue charges
(primarily Aurora Serverless v2). Run cleanup.ps1 as soon as you are done.
"@ | Write-Host
