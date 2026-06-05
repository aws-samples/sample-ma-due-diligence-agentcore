# Requirements Document

## Introduction

This document defines the requirements for an open-source AWS sample that demonstrates a multi-agent system for M&A due diligence in the transportation and logistics industry. The sample accompanies an AWS Machine Learning blog post and must enable readers to deploy, run, and tear down a working system with minimal friction.

The sample uses Amazon Bedrock AgentCore Runtime to host four specialist agents coordinated by a supervisor agent, all built with the Strands SDK. Agents ground their responses in synthetic M&A documents via Knowledge Bases for Amazon Bedrock, retain context through AgentCore Memory, and are subject to safety controls via AWS Bedrock Guardrails and a custom citation-validation evaluator.

### In Scope (v1)

- Multi-agent orchestration using Strands SDK on Amazon Bedrock AgentCore Runtime
- Four specialist agents coordinated by a supervisor: Target Screening, Financial Analysis, Strategic Fit, Compliance Validation
- RAG over synthetic M&A documents via Knowledge Bases for Amazon Bedrock
- Text-to-SQL over structured target-company data in AWS Aurora PostgreSQL Serverless v2
- Persistent context via AgentCore Memory
- Session and cache storage in AWS DynamoDB
- Safety controls via AWS Bedrock Guardrails
- One custom evaluator demonstrating citation validation
- One external tool via AgentCore Gateway demonstrating the MCP pattern
- Observability via Amazon CloudWatch and AWS X-Ray
- Notebook and CLI interfaces over a shared Python package
- Synthetic data generator and example prompts
- Infrastructure as Code deployment and teardown

### Out of Scope (v1)

- React frontend, CloudFront, Amazon Cognito (reference the FAST template instead)
- Additional Gateway integrations beyond the single demonstration tool
- AgentCore Identity with JWT validation flows
- Cedar policies
- KMS customer-managed keys, PrivateLink, AWS Config rules (Aurora is placed in a VPC, but additional network-isolation controls are deferred)
- Production-grade CI/CD, multi-region deployment, or HA configuration

## Requirements

### Requirement 1: Multi-Agent Orchestration

**User Story:** A reader following the walkthrough needs to interact with a coordinated set of specialist agents, enabling them to see how multi-agent collaboration handles M&A due diligence tasks.

#### Acceptance Criteria

1. WHEN the system is deployed THEN it SHALL provide exactly one supervisor agent that routes prompts to specialist agents.
2. WHEN the system is deployed THEN it SHALL provide four specialist agents: Target Screening, Financial Analysis, Strategic Fit, and Compliance Validation.
3. WHERE an agent is implemented THE system SHALL use the Strands SDK.
4. WHERE an agent runs THE system SHALL host it on Amazon Bedrock AgentCore Runtime.
5. WHEN the supervisor receives a prompt THEN it SHALL invoke one or more specialists using the agents-as-tools pattern based on prompt intent.
6. WHEN a specialist returns a response grounded in retrieved documents THEN that response SHALL include inline citations resolvable to source documents.

### Requirement 2: Knowledge Retrieval, Structured Data, and Memory

**User Story:** A reader needs agents to ground their answers in synthetic organizational documents, query structured target data, and retain context across turns, enabling them to validate the blog's claims about RAG, text-to-SQL, and knowledge retention.

#### Acceptance Criteria

1. WHEN the sample is deployed THEN the system SHALL index synthetic M&A documents in an Knowledge Bases for Amazon Bedrock backed by Amazon S3.
2. WHEN an agent response is grounded in indexed documents THEN the response SHALL return citations resolvable to the source S3 objects.
3. WHERE session context must persist across turns THE system SHALL use AgentCore Memory.
4. WHEN the Strategic Fit agent runs THEN it SHALL have access to prior-deal memos via AgentCore Memory.
5. WHERE structured target company data (e.g., revenue, EBITDA, fleet size, geographic coverage) is stored THE system SHALL use AWS Aurora PostgreSQL Serverless v2.
6. WHERE session data and cached invocation metadata are stored THE system SHALL use AWS DynamoDB.

### Requirement 2a: Text-to-SQL

**User Story:** A reader needs the Target Screening agent to answer natural-language questions about structured target data, enabling them to see how agents translate user intent into auditable SQL queries.

#### Acceptance Criteria

1. WHEN the Target Screening agent receives a natural-language query about structured target data THEN it SHALL translate the query into a SQL statement.
2. WHEN the translated SQL is produced THEN the agent SHALL run it against the Aurora PostgreSQL database and return the results.
3. WHEN a text-to-SQL query is run THEN the SQL statement that ran SHALL be visible in the agent trace for auditability.
4. WHERE SQL is generated from user input THE system SHALL apply parameterized query patterns or equivalent safeguards to mitigate SQL injection risks.
5. WHERE Aurora is accessed by agents THE access SHALL use an IAM-authenticated database role with read-only permissions on the target-company schema.

### Requirement 3: External Tool Integration

**User Story:** The reader must be able to see how agents invoke external tools through AgentCore Gateway, enabling them to understand the MCP-based integration pattern described in the blog.

#### Acceptance Criteria

1. WHEN the sample is deployed THEN it SHALL expose at least one external tool through AgentCore Gateway.
2. WHERE the external tool is backed by an implementation THE system SHALL use an AWS Lambda function returning deterministic synthetic data.
3. WHEN at least one specialist agent runs its normal flow THEN it SHALL invoke the external tool.

### Requirement 4: Safety Controls and Evaluation

**User Story:** The reader must be able to see concrete safety controls and output validation, enabling them to trust the blog's governance narrative.

#### Acceptance Criteria

1. WHERE the supervisor agent processes requests THE system SHALL apply an AWS Bedrock Guardrail configured to filter harmful content and enforce financial-advice disclaimers.
2. WHEN an agent returns a response THEN the system SHALL provide a custom evaluator that validates every factual claim has at least one supporting citation.
3. WHEN a user runs the evaluator from the notebook or CLI THEN the evaluator SHALL produce a pass/fail result with per-claim detail.
4. WHEN the evaluator produces a result THEN the result SHALL be stored alongside the agent response for auditability.

### Requirement 5: Synthetic Data

**User Story:** A reader needs a ready-made synthetic dataset, enabling them to run the example prompts end-to-end without sourcing my own M&A documents.

#### Acceptance Criteria

1. WHEN the data generator runs THEN it SHALL produce at least 3 target-company Confidential Information Memoranda (CIMs), summary financials, press releases, and prior-deal memos.
2. WHEN the data generator runs THEN it SHALL produce a seed dataset of at least 20 fictional transportation and logistics target companies with structured attributes.
3. WHERE synthetic documents reference companies THE content SHALL be clearly labeled as synthetic and SHALL NOT reference real companies without disclaimers.
4. WHEN the data generator runs THEN it SHALL populate Amazon S3, Knowledge Bases for Amazon Bedrock, AWS Aurora PostgreSQL, AWS DynamoDB, and AgentCore Memory.
5. WHERE the data generator is invoked THE system SHALL support invocation either before or after deployment of the agent stack.

### Requirement 6: Example Prompts

**User Story:** A reader needs ready-to-run prompts for each specialist agent, enabling them to reproduce the blog's demonstrations without designing prompts myself.

#### Acceptance Criteria

1. WHEN the sample is delivered THEN it SHALL include four ready-to-run prompts, one per specialist agent.
2. WHEN an example prompt is run against the synthetic dataset THEN it SHALL produce a meaningful, citation-backed response.
3. WHERE example prompts are documented THE sample SHALL provide them in a standalone `prompts.md` file and embed them in the walkthrough notebook.

### Requirement 7: User Interfaces

**User Story:** A reader needs a notebook that mirrors the blog narrative and a CLI for scripting, enabling them to both learn the system and integrate it into my own tooling.

#### Acceptance Criteria

1. WHEN the sample is delivered THEN it SHALL include a primary Jupyter notebook at `notebooks/walkthrough.ipynb`.
2. WHEN the notebook is run top-to-bottom THEN it SHALL include cells for: environment validation, data overview, one scenario per specialist agent, and an optional trace-inspection cell.
3. WHEN the sample is delivered THEN it SHALL include a CLI entry point supporting the following commands: invoke a named agent with a prompt, list available agents, and print the trace for the most recent invocation.
4. WHERE both the notebook and the CLI invoke agents THE system SHALL route invocations through a shared Python package (`mna/`) without duplicating logic.

### Requirement 8: Deployment and Cleanup

**User Story:** A reader needs one-command deploy and cleanup, enabling them to try the sample without risking orphaned resources or unexpected charges.

#### Acceptance Criteria

1. WHEN a reader runs the deploy script THEN the system is designed to deploy end-to-end with a single command (`deploy.sh` on macOS/Linux, `deploy.ps1` on Windows).
2. WHEN deployment runs for the first time in a bootstrapped account THEN it SHALL complete in under 25 minutes (accounting for Aurora Serverless v2 provisioning).
3. WHEN a reader runs the cleanup script THEN the system SHALL tear down end-to-end with a single command (`cleanup.sh` / `cleanup.ps1`).
4. WHEN cleanup completes THEN the cleanup process is designed to remove billable resources created by this sample. WARNING: Cleanup permanently deletes all data in S3, DynamoDB, Aurora, and AgentCore Memory. This action covers billable resources created by the sample SHALL be removed, including S3 buckets, Knowledge Base, DynamoDB tables, Lambda functions, AgentCore Runtime, AgentCore Memory, and Guardrail.
5. WHERE cleanup verification is needed THE README SHALL document specific commands the reader can run to confirm teardown completed.

### Requirement 9: Observability

**User Story:** The reader must be able to inspect how requests flow through the agents, enabling them to understand and debug the system's behavior.

#### Acceptance Criteria

1. WHEN any agent is invoked THEN the system SHALL produce structured logs in Amazon CloudWatch.
2. WHEN any agent is invoked THEN the system SHALL produce an AWS X-Ray trace covering supervisor, specialist, and tool/Knowledge Base calls.
3. WHEN the notebook's trace-inspection cell is run THEN it SHALL retrieve and display the trace for the most recent invocation.

### Requirement 10: Operating System and Regional Availability

**User Story:** A reader needs the sample to fail fast if my environment is unsupported, so that I don't waste time debugging avoidable errors.

#### Acceptance Criteria

1. WHERE the sample is deployed THE target AWS region SHALL be one where Amazon Bedrock AgentCore is generally available.
2. WHEN deployment is attempted in an unsupported region THEN the system SHALL fail fast with a clear error message and link to the AgentCore regions documentation.
3. WHEN the README is delivered THEN it SHALL document all supported regions.

### Requirement 11: Runtime and Dependencies

**User Story:** A maintainer needs a controlled dependency surface, so that the sample remains reproducible and maintainable.

#### Acceptance Criteria

1. WHERE agent runtime code runs THE system SHALL use Python 3.11 or higher.
2. WHERE infrastructure is defined THE system SHALL use AWS CDK v2 (TypeScript or Python, consistently applied).
3. WHEN dependencies are declared THEN they SHALL be pinned (via `requirements.txt` for Python and `package-lock.json` for Node).
4. WHEN a reader deploys the sample THEN the deployment is designed to not require Docker, WSL, or any container runtime on the reader's machine.
5. WHERE the agent container image is built THE build SHALL run in AWS CodeBuild using a managed ARM64 build environment.
6. WHEN a first-time Windows 11 reader has only AWS CLI v2, Python 3.11, Node.js 20, and PowerShell installed THEN they SHALL be able to run `.\deploy.ps1`, execute all four example prompts, and run `.\cleanup.ps1` to completion without installing any additional tool.
7. WHERE the repository contains scripts THE repository SHALL include platform-native pairs (`*.ps1` for Windows, `*.sh` for macOS/Linux) with feature parity.

### Requirement 11a: CloudFormation Custom Resource Safety

**User Story:** A reader needs CloudFormation stack operations to succeed or fail fast, so that I never have to wait hours for a stuck stack deletion.

#### Acceptance Criteria

1. WHERE a Lambda-backed Custom Resource is implemented THE Lambda module top-level code is designed to not raise under normal operating conditions (no SDK imports, client construction, or environment lookups at import time).
2. WHEN a Custom Resource handler is invoked THEN it SHALL wrap all business logic in a `try / except / finally` block that sends a response to CloudFormation in the `finally` clause.
3. WHERE a Custom Resource response is sent THE system SHALL use raw `urllib.request` (not boto3) so import or runtime failures do not prevent the response.
4. WHEN a Custom Resource handler fails for any reason THEN it SHALL return a `FAILED` status to CloudFormation within the Lambda's invocation window (maximum 15 minutes) with a reason string including the exception type and the CloudWatch log stream name.
5. WHERE a Custom Resource polls for asynchronous work completion THE polling SHALL cap total wait time at 14 minutes and respond with a clear `FAILED` status on timeout.
6. WHEN a Custom Resource processes a `Delete` request for a resource that does not exist THEN it SHALL treat the missing resource as success.
7. WHERE Custom Resource response data is returned THE `Data` field SHALL remain under 4 KB, with larger values replaced by CloudWatch log references.
8. WHERE multiple Custom Resources exist THE project SHALL provide a shared `lambda/_cr_common/send_response.py` module that implements response-sending and enforces rules 1–3, 7.
9. WHEN a Custom Resource handler is unit tested THEN the tests SHALL cover at minimum: simulated import error, runtime exception, successful Create, successful Update, and successful Delete-of-missing-resource; each SHALL produce a well-formed CloudFormation response body.

### Requirement 12: Foundation Model Configuration

**User Story:** The reader must be able to swap foundation models easily, enabling them to experiment with performance, cost, and capability trade-offs.

#### Acceptance Criteria

1. WHEN a reader configures the sample THEN the supervisor model SHALL be configurable via a single environment variable or CDK context value.
2. WHEN the sample is delivered THEN the default specialist models SHALL be documented with a rationale for each choice.
3. WHERE the sample invokes foundation models THE system SHALL use on-demand Bedrock pricing and SHALL NOT require Provisioned Throughput.

### Requirement 13: Cost

**User Story:** A reader needs predictable, low cost to try the sample, enabling them to evaluate the solution without budget concerns.

#### Acceptance Criteria

1. WHEN a first-time reader deploys the stack, runs the four example prompts, and tears down the stack in a default region THEN the total AWS cost SHALL be under $5 USD.
2. WHEN the README is delivered THEN it SHALL include a cost estimate table broken down by service, with Aurora Serverless v2 listed as a distinct line item.
3. WHERE Aurora Serverless v2 is provisioned THE minimum ACU capacity SHALL be set to the lowest supported value to minimize idle cost.

### Requirement 14: Security

**User Story:** As a security-conscious reader needs the sample to follow least-privilege defaults, enabling them to adopt it without introducing security issues.

#### Acceptance Criteria

1. WHERE IAM roles are created THE roles SHALL follow least privilege and SHALL be documented in the README.
2. WHEN the sample is delivered THEN it SHALL NOT contain hardcoded credentials, API keys, or account IDs.
3. WHERE Amazon S3 buckets are created THE buckets SHALL block public access by default.
4. WHERE data is stored at rest THE system SHALL encrypt it using AWS-managed keys (customer-managed keys called out as a production extension).
5. WHERE synthetic data is generated THE data SHALL NOT contain real PII or real company financial data.
6. WHERE Aurora PostgreSQL is deployed THE database SHALL be placed in private subnets with no public internet access.
7. WHERE Aurora credentials are required THE system SHALL use IAM database authentication or AWS Secrets Manager, not hardcoded passwords.

### Requirement 15: Code Quality

**User Story:** A maintainer needs consistent code quality, so that contributions remain easy to review and the sample stays trustworthy.

#### Acceptance Criteria

1. WHEN Python code is committed THEN it SHALL pass `ruff` or `flake8` linting with project-defined rules.
2. WHEN CDK code is synthesized THEN `cdk synth` SHALL complete without warnings.
3. WHEN the sample is delivered THEN it SHALL include a `tests/smoke_test.py` that verifies deployment by invoking each agent once.
4. WHEN the repository is delivered THEN it SHALL include a `CONTRIBUTING.md` and a `LICENSE` file using MIT-0.

### Requirement 16: Documentation

**User Story:** A reader needs complete documentation, enabling them to deploy, understand, extend, and troubleshoot the sample without external help.

#### Acceptance Criteria

1. WHEN the README is delivered THEN it SHALL cover: overview, architecture diagram, prerequisites, deploy, run, cleanup, cost, troubleshooting, and extension points.
2. WHEN the README is delivered THEN it SHALL link to the companion blog post and to the official AgentCore, Strands, and Knowledge Bases for Amazon Bedrock documentation.
3. WHERE a specialist agent source file is delivered THE file SHALL include a module docstring describing the agent's role, tools, and an example prompt.
4. WHEN the README is delivered THEN it SHALL include a clearly labeled "what's implemented vs. what's an extension" table mapped to the architecture diagram.

### Requirement 17: Maintainability

**User Story:** A maintainer needs a clean project structure, so that future updates do not require invasive refactors.

#### Acceptance Criteria

1. WHERE the repository layout is established THE repository SHALL have fewer than 10 top-level directories.
2. WHERE agent logic and infrastructure code coexist THE two SHALL be separated such that an agent can be modified without changing the CDK stacks.
3. WHEN dependencies are selected THEN the sample SHALL NOT depend on experimental or unreleased SDK features.

## Acceptance Criteria (Sample-Level)

The sample is considered complete when all of the following are true:

1. A new user, following only the README, should be able to deploy the stack, run all four example prompts successfully, and tear down the stack without contacting support in typical scenarios.
2. Each of the four example prompts produces a response with at least one citation resolvable to an S3 object in the Knowledge Base.
3. The custom evaluator runs against all four example responses and reports a pass/fail for each.
4. At least one specialist agent invocation produces an X-Ray trace showing a call through AgentCore Gateway to the Lambda-backed external tool.
5. Deploy-to-cleanup on a fresh AWS account takes less than 30 minutes of wall time and under $5 of spend.
6. The smoke test passes in CI on a clean environment.
7. The companion blog post can reference each numbered requirement as a concrete capability the reader will experience.

## Dependencies and Assumptions

1. Readers have an AWS account with AWS Bedrock model access enabled for Anthropic Claude and Amazon Nova model families.
2. Readers have AWS CLI v2 configured with credentials that grant CloudFormation, Lambda, S3, DynamoDB, IAM, Bedrock, and AgentCore permissions.
3. Amazon Bedrock AgentCore remains generally available in at least one commercial AWS region at the time of publication.
4. The Strands SDK remains the recommended orchestration framework for AgentCore at the time of publication.

## Conclusion

These requirements define the scope, acceptance criteria, and constraints for the M&A Due Diligence Multi-Agent sample. Together they are designed to make the sample deployable, demonstrable, safe, and cost-controlled. The design document translates these requirements into a concrete architecture, and the implementation plan maps each requirement to an executable task.
