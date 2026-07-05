#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# deploy.sh
#
# Purpose
#   One-command end-to-end deployment of the M&A Due Diligence AgentCore
#   sample on macOS/Linux. POSIX-bash sibling of deploy.ps1 (feature
#   parity enforced by Requirement NFR-RT-7 / 11.7).
#
# What this script does (in order)
#   1. Preflight: region check. Exits non-zero with an actionable error
#      message, so deployment fails fast before any billable resource
#      is created (Requirement 10.2).
#   2. Set up a local Python virtual environment under `.venv/`,
#      install pinned dependencies from requirements.txt, and install
#      this project itself in editable mode (`pip install -e .`) so the
#      `mna` package and its `mna` console-script entry point are
#      available for data/generate.py, tests/smoke_test.py, the
#      notebook, and Step 2 of the walkthrough. Keeps the reader's
#      global Python clean (design §"Virtual environment").
#   3. `cdk bootstrap` — idempotent. Skipped on accounts where the
#      bootstrap stack is already present.
#   4. `cdk deploy --all` — CDK resolves the Network → Data → Evaluator
#      → Gateway → Agent dependency order from the stack graph declared
#      in infra/app.py (Requirement 8.1, 8.2).
#   5. Seed synthetic data via `python data/generate.py --seed-all`.
#      Resource identifiers are resolved from SSM inside generate.py so
#      no extra arguments are needed here.
#   6. Print copy-paste instructions to open the walkthrough notebook.
#
# Exit codes
#   0  success
#   1  preflight or deployment failure (terminates at the failing step)
#
# Optional flags
#   --skip-preflight   Skip scripts/check_region.sh. Handy for re-runs
#                      where the reader already confirmed the environment.
#   --skip-seed        Skip `data/generate.py --seed-all`. Useful if the
#                      reader plans to run the generator manually with
#                      custom overrides.
#   --skip-venv        Use the active Python instead of creating `.venv`.
#                      Recommended only inside CI where the runner image
#                      already has the pinned requirements.
#   --skip-smoke       Skip `tests/smoke_test.py`. Useful when deploying
#                      into a preview region where the smoke-test
#                      assertions may not yet hold.
#
# Requirements mapping: 8.1, 8.2, NFR-RT-6, NFR-RT-7 (feature parity).
# ---------------------------------------------------------------------------

set -euo pipefail

# Resolve the directory this script lives in so the script works when
# invoked from anywhere (e.g. `bash /some/path/deploy.sh`).
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$REPO_ROOT"

SKIP_PREFLIGHT=0
SKIP_SEED=0
SKIP_VENV=0
SKIP_SMOKE=0

for arg in "$@"; do
  case "$arg" in
    --skip-preflight) SKIP_PREFLIGHT=1 ;;
    --skip-seed)      SKIP_SEED=1 ;;
    --skip-venv)      SKIP_VENV=1 ;;
    --skip-smoke)     SKIP_SMOKE=1 ;;
    -h|--help)
      sed -n '2,50p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
      exit 0
      ;;
    *)
      echo "ERROR: Unknown argument '$arg'. Use --help for usage." >&2
      exit 1
      ;;
  esac
done

log() { printf '\n[deploy] %s\n' "$*"; }

# ---------------------------------------------------------------------------
# Step 1: Preflight
# ---------------------------------------------------------------------------
if [ "$SKIP_PREFLIGHT" -eq 0 ]; then
  log "Step 1/7: Preflight checks (region)"
  bash "$REPO_ROOT/scripts/check_region.sh"
else
  log "Step 1/7: Preflight checks skipped (--skip-preflight)"
fi

# ---------------------------------------------------------------------------
# Step 2: Virtual environment + dependencies
# ---------------------------------------------------------------------------
if [ "$SKIP_VENV" -eq 0 ]; then
  log "Step 2/7: Virtual environment and Python dependencies"
  if [ ! -d "$REPO_ROOT/.venv" ]; then
    log "Creating .venv"
    # Prefer python3.11; fall back to python3 so the script still works
    # on hosts that only expose an unversioned interpreter name.
    if command -v python3.11 >/dev/null 2>&1; then
      python3.11 -m venv "$REPO_ROOT/.venv"
    elif command -v python3 >/dev/null 2>&1; then
      python3 -m venv "$REPO_ROOT/.venv"
    else
      echo "ERROR: python3 not found on PATH." >&2
      exit 1
    fi
  else
    log ".venv already present — reusing"
  fi

  # shellcheck disable=SC1091
  source "$REPO_ROOT/.venv/bin/activate"

  log "Upgrading pip and installing pinned requirements.txt"
  python -m pip install --quiet --upgrade pip
  python -m pip install --quiet -r "$REPO_ROOT/requirements.txt"

  # Install this project in editable mode so the `mna` package (used by
  # data/generate.py, tests/smoke_test.py, and the notebook) and the
  # `mna` console-script entry point (used in Step 2 of the walkthrough)
  # are both available. Without this, `mna invoke ...` is not found on
  # PATH and `data/generate.py --seed-all` fails with
  # "ModuleNotFoundError: No module named 'mna'".
  log "Installing project in editable mode (pip install -e .)"
  python -m pip install --quiet -e "$REPO_ROOT"

  # --------------------------------------------------------------
  # Vendor boto3 into lambda/_vendor (see deploy.ps1 for rationale).
  # --------------------------------------------------------------
  VENDOR_DIR="$REPO_ROOT/lambda/_vendor"
  LAMBDA_REQ="$REPO_ROOT/lambda/requirements.txt"
  VENDOR_STAMP="$VENDOR_DIR/.pinned-from"
  DESIRED_STAMP=$(tr -d '[:space:]' < "$LAMBDA_REQ")
  EXISTING_STAMP=""
  if [ -f "$VENDOR_STAMP" ]; then
    EXISTING_STAMP=$(tr -d '[:space:]' < "$VENDOR_STAMP")
  fi
  if [ "$DESIRED_STAMP" != "$EXISTING_STAMP" ]; then
    log "Installing lambda/requirements.txt into lambda/_vendor"
    rm -rf "$VENDOR_DIR"
    mkdir -p "$VENDOR_DIR"
    python -m pip install --quiet --no-compile -r "$LAMBDA_REQ" -t "$VENDOR_DIR"
    printf '%s' "$DESIRED_STAMP" > "$VENDOR_STAMP"
  else
    log "lambda/_vendor already matches lambda/requirements.txt — reusing"
  fi
else
  log "Step 2/7: Virtual environment skipped (--skip-venv)"
fi

# ---------------------------------------------------------------------------
# Step 3: cdk bootstrap (idempotent)
# ---------------------------------------------------------------------------
log "Step 3/7: cdk bootstrap (idempotent)"

# Resolve the target region from the CLI/env so the CDKToolkit stack
# check probes the right place. check_region.sh already validated the
# value if preflight was run.
REGION="${AWS_REGION:-${AWS_DEFAULT_REGION:-}}"
if [ -z "$REGION" ] && command -v aws >/dev/null 2>&1; then
  REGION="$(aws configure get region 2>/dev/null || true)"
fi
if [ -z "$REGION" ]; then
  echo "ERROR: No AWS region configured; cannot run cdk bootstrap." >&2
  exit 1
fi

# Skip bootstrap when the CDKToolkit CloudFormation stack already exists
# in the region. Speeds up re-runs and avoids spurious "no changes" noise.
if aws cloudformation describe-stacks \
      --region "$REGION" \
      --stack-name CDKToolkit \
      --query 'Stacks[0].StackStatus' \
      --output text >/dev/null 2>&1; then
  log "CDKToolkit stack already bootstrapped in $REGION — skipping"
else
  (
    cd "$REPO_ROOT/infra"
    cdk bootstrap
  )
fi

# ---------------------------------------------------------------------------
# Step 4: cdk deploy --all
# ---------------------------------------------------------------------------
log "Step 4/7: cdk deploy --all (Network → Data → Evaluator → Gateway → Agent)"
# --require-approval never suppresses the interactive IAM prompt so the
# script stays one-command. CDK still prints the change set.
(
  cd "$REPO_ROOT/infra"
  cdk deploy --all --require-approval never
)

# ---------------------------------------------------------------------------
# Step 5: Seed synthetic data
# ---------------------------------------------------------------------------
if [ "$SKIP_SEED" -eq 0 ]; then
  log "Step 5/7: Seeding synthetic data (data/generate.py --seed-all)"
  python "$REPO_ROOT/data/generate.py" --seed-all
else
  log "Step 5/7: Data seeding skipped (--skip-seed)"
  log "Run manually later with:  python data/generate.py --seed-all"
fi

# ---------------------------------------------------------------------------
# Step 6: Post-deploy smoke test
# ---------------------------------------------------------------------------
# Task 38 designates tests/smoke_test.py as the final verification
# step of the deploy script. It exercises each specialist with its
# prompts.md prompt, asserts the evaluator returns a pass/fail for
# each, and confirms at least one invocation's X-Ray trace includes
# a Gateway hop (Requirement 15.3, Sample-level AC 2-4).
if [ "$SKIP_SMOKE" -eq 0 ]; then
  log "Step 6/7: Running post-deploy smoke test"
  # Don't stop the script on a smoke-test failure — the stack is
  # deployed, the reader may want to inspect it manually. Print a
  # clear warning instead and continue to the next-steps message.
  if python -m pytest "$REPO_ROOT/tests/smoke_test.py" -m smoke --no-header -ra; then
    log "Smoke test PASSED"
  else
    log "Smoke test FAILED — inspect the output above. The stack is still deployed."
    log "You can re-run the smoke test with:  python -m pytest tests/smoke_test.py -m smoke"
  fi
else
  log "Step 6/7: Smoke test skipped (--skip-smoke)"
fi

# ---------------------------------------------------------------------------
# Step 7: Next-steps message
# ---------------------------------------------------------------------------
log "Step 7/7: Deployment complete"
cat <<EOF

Next steps:
  1. Open the walkthrough notebook:
       jupyter lab notebooks/walkthrough.ipynb
     (or "jupyter notebook notebooks/walkthrough.ipynb")

  2. Alternatively, invoke an agent from the CLI:
       python -m cli.invoke list-agents
       python -m cli.invoke invoke supervisor "Screen mid-market logistics targets."

  3. Tear down all billable resources when you are done:
       ./cleanup.sh

Cost reminder: leaving the stack deployed continues to accrue charges
(primarily Aurora Serverless v2). Run cleanup.sh as soon as you are done.
EOF
