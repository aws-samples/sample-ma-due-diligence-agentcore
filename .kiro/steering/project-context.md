# Project Context

## AWS Configuration

- **AWS_PROFILE**: `sam`
- **AWS_REGION**: `us-east-1`

Always use `--profile sam` when running AWS CLI commands or set
`AWS_PROFILE=sam` in the environment. Always target `us-east-1`.

## Active Branch

`feat/agentcore-l2-streaming` — upgrading from Custom Resource-backed
AgentCore provisioning to stable L2 constructs, adding citation
collection, and switching the supervisor to async streaming.

## Upgrade Reference

See `RELEASE_NOTES.md` at the repo root for the full change manifest.
