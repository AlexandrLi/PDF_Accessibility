# IAM Permissions Required for PDF Accessibility Solutions

This document outlines the IAM permissions required to deploy and operate each PDF accessibility solution.

Deployment policies are maintained as standalone JSON files in [`policies/`](../policies/):

| File | Type | Purpose |
|------|------|---------|
| [`deploy-caller-policy.json`](../policies/deploy-caller-policy.json) | Identity policy | Must be manually attached to the IAM user/role running `deploy.sh` |
| [`pdf2pdf-codebuild-policy.json`](../policies/pdf2pdf-codebuild-policy.json) | Identity policy | Loaded by `deploy.sh` and attached to the CodeBuild service role (pdf2pdf) |
| [`pdf2html-codebuild-policy.json`](../policies/pdf2html-codebuild-policy.json) | Identity policy | Loaded by `deploy.sh` and attached to the CodeBuild service role (pdf2html) |
| [`codebuild-trust-policy.json`](../policies/codebuild-trust-policy.json) | Trust policy | Loaded by `deploy.sh` when creating the CodeBuild service role |

Validate any policy with:
```bash
aws accessanalyzer validate-policy \
  --policy-document file://policies/pdf2pdf-codebuild-policy.json \
  --policy-type IDENTITY_POLICY
```

---

## Caller Permissions (User Running `deploy.sh`)

The user or role that runs `deploy.sh` makes AWS API calls *before* CodeBuild starts. This includes both the backend deploy script and the UI deploy script (from the [PDF_accessability_UI](https://github.com/ASUCICREPO/PDF_accessability_UI) repo).

See [`policies/deploy-policy.json`](../policies/deploy-policy.json) for the full document.

| Sid | Actions | Resources | Purpose |
|-----|---------|-----------|---------|
| CloudShellAccess | `cloudshell:*` | `*` | Access AWS CloudShell environment |
| STSAccess | `sts:GetCallerIdentity` | `*` | Verify AWS credentials |
| SecretsManagerAccess | `secretsmanager:CreateSecret`, `UpdateSecret` | `secret:/myapp/*` | Store Adobe API credentials (pdf2pdf only) |
| BedrockDataAutomationAccess | `bedrock:CreateDataAutomationProject` | `*` | Create BDA project (pdf2html only) |
| IAMRoleManagement | `iam:GetRole`, `CreateRole`, `CreatePolicy`, `GetPolicy`, `AttachRolePolicy`, `PutRolePolicy` | `role/*-codebuild-service-role`, `role/pdf-ui-*-service-role`, `policy/*` | Create CodeBuild service roles and policies (backend + UI) |
| IAMPassRoleToCodeBuild | `iam:PassRole` | `role/*-codebuild-service-role`, `role/pdf-ui-*-service-role` (conditioned on `iam:PassedToService`: codebuild) | Pass role to CodeBuild projects |
| CodeBuildAccess | `codebuild:CreateProject`, `StartBuild`, `BatchGetBuilds` | `project/pdfremediation-*`, `project/pdf-ui-*` | Create and monitor CodeBuild projects (backend + UI) |
| CloudWatchLogsAccess | `logs:DescribeLogStreams`, `GetLogEvents` | `log-group:/aws/codebuild/*` | Read build logs on failure |
| CloudFormationReadAccess | `cloudformation:DescribeStacks`, `ListStacks` | `*` | Retrieve stack outputs (bucket names, Cognito IDs, Amplify URLs) |
| S3ListBuckets | `s3:ListAllMyBuckets` | `*` | Find deployed bucket by name pattern |

---

## Deployment Permissions (CodeBuild Role)

The deploy script (`deploy.sh`) creates a CodeBuild service role and attaches a scoped IAM policy. The trust policy and identity policies are read from the `policies/` directory.

### PDF-to-PDF Deployment Policy

See [`policies/pdf2pdf-codebuild-policy.json`](../policies/pdf2pdf-codebuild-policy.json) for the full document.

| Sid | Actions | Resources | Purpose |
|-----|---------|-----------|---------|
| S3Access | `s3:*` | `cdk-*`, `pdfaccessibility*` | CDK assets and application bucket |
| ECRAccess | `ecr:*` | `repository/cdk-*` | CDK ECR image assets |
| ECRAuth | `ecr:GetAuthorizationToken` | `*` | Docker login to ECR |
| LambdaAccess | `lambda:*` | `function:*` | Create/update Lambda functions |
| ECSAccess | `ecs:*` | `*` | ECS cluster, task definitions, services |
| EC2Access | `ec2:*` | `*` | VPC, subnets, NAT gateways, endpoints |
| StepFunctionsAccess | `states:*` | `stateMachine:*` | Step Functions state machines |
| IAMRoleAccess | 16 IAM role actions | `role/PDFAccessibility*`, `role/cdk-*` | Stack and CDK roles |
| IAMPolicyAccess | 7 IAM policy actions | `policy/*` | Managed policies |
| CloudFormationAccess | `cloudformation:*` | `PDFAccessibility*/*`, `CDKToolkit/*` | CDK stack deployment |
| LogsAccess | `logs:*` | CodeBuild, Lambda, ECS, Step Functions log groups | CloudWatch Logs |
| CloudWatchAccess | 4 CloudWatch actions | `*` | Metrics and dashboards |
| SecretsManagerAccess | 4 Secrets Manager actions | `secret:/myapp/*` | Adobe API credentials |
| STSAccess | `GetCallerIdentity`, `AssumeRole` | `*` | Identity and CDK role assumption |
| SSMAccess | 3 SSM actions | `parameter/cdk-bootstrap/*` | CDK bootstrap parameters |
| CodeConnectionsAccess | `UseConnection`, `GetConnection` | `connection/*` | GitHub source connection |

### PDF-to-HTML Deployment Policy

See [`policies/pdf2html-codebuild-policy.json`](../policies/pdf2html-codebuild-policy.json) for the full document.

| Sid | Actions | Resources | Purpose |
|-----|---------|-----------|---------|
| S3Access | `s3:*` | `cdk-*`, `pdf2html-*` | CDK assets and application bucket |
| ECRAccess | `ecr:*` | `repository/cdk-*`, `repository/pdf2html-*` | CDK and Lambda container images |
| ECRAuth | `ecr:GetAuthorizationToken` | `*` | Docker login to ECR |
| LambdaAccess | `lambda:*` | `function:Pdf2Html*`, `function:pdf2html*` | Create/update Lambda functions |
| IAMRoleAccess | 16 IAM role actions | `role/Pdf2Html*`, `role/pdf2html*`, `role/cdk-*` | Stack and CDK roles |
| IAMPolicyAccess | 7 IAM policy actions | `policy/*` | Managed policies |
| CloudFormationAccess | `cloudformation:*` | `Pdf2Html*/*`, `pdf2html*/*`, `CDKToolkit/*` | CDK stack deployment |
| BedrockAccess | 5 BDA project actions | `*` | Create/manage Bedrock Data Automation project |
| LogsAccess | `logs:*` | CodeBuild and Lambda log groups | CloudWatch Logs |
| STSAccess | `GetCallerIdentity`, `AssumeRole` | `*` | Identity and CDK role assumption |
| SSMAccess | 3 SSM actions | `parameter/cdk-bootstrap/*` | CDK bootstrap parameters |
| CodeConnectionsAccess | `UseConnection`, `GetConnection` | `connection/*` | GitHub source connection |

---

## Runtime Permissions — PDF-to-PDF

These permissions are created by the CDK stack (`app.py`) and attached to roles at processing time.

### Required AWS Services
- Amazon S3 — File storage and processing
- AWS Lambda — Serverless compute (PDF splitter, merger, title generator, accessibility checkers)
- Amazon ECS (Fargate) — Containerized processing (Adobe Autotag, Alt-Text Generator)
- Amazon ECR — Container image registry
- AWS Step Functions — Workflow orchestration
- Amazon EC2 — VPC and networking infrastructure
- Amazon Bedrock — AI/ML model invocation
- AWS Secrets Manager — Adobe API credentials storage
- Amazon CloudWatch — Monitoring, logging, and dashboards
- Amazon Comprehend — Language detection

### ECS Task Role

```json
{
  "Statement": [
    {
      "Sid": "BedrockInvokeModel",
      "Effect": "Allow",
      "Action": ["bedrock:InvokeModel"],
      "Resource": "*"
    },
    {
      "Sid": "S3BucketAccess",
      "Effect": "Allow",
      "Action": ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"],
      "Resource": ["arn:aws:s3:::${BucketName}", "arn:aws:s3:::${BucketName}/*"]
    },
    {
      "Sid": "ComprehendLanguageDetection",
      "Effect": "Allow",
      "Action": ["comprehend:DetectDominantLanguage"],
      "Resource": "*"
    },
    {
      "Sid": "SecretsManagerAccess",
      "Effect": "Allow",
      "Action": ["secretsmanager:GetSecretValue"],
      "Resource": "arn:aws:secretsmanager:${Region}:${AccountId}:secret:/myapp/*"
    }
  ]
}
```

> The ECS Task Execution Role also receives `AmazonECSTaskExecutionRolePolicy` (AWS managed) and S3 read/write on the processing bucket via `grant_read_write`.

### Lambda Function Permissions

All Lambda functions receive:
- S3 read/write on the processing bucket via `grant_read_write`
- `cloudwatch:PutMetricData` on `*` (no resource-level support)

Additional per-function permissions:

| Function | Extra Permissions | Resource |
|----------|-------------------|----------|
| Title Generator | `bedrock:InvokeModel` | `*` |
| Pre-Remediation Checker | `secretsmanager:GetSecretValue` | `secret:/myapp/*` |
| Post-Remediation Checker | `secretsmanager:GetSecretValue` | `secret:/myapp/*` |
| PDF Splitter | `states:StartExecution` | State machine ARN (via `grant_start_execution`) |

---

## Runtime Permissions — PDF-to-HTML

These permissions are created by the CDK stack (`pdf2html/cdk/lib/pdf2html-stack.js`).

### Required AWS Services
- Amazon S3 — File storage and processing
- AWS Lambda — Serverless compute
- Amazon ECR — Container image registry
- Amazon Bedrock — Model invocation and Data Automation
- Amazon CloudWatch — Monitoring and logging

### Lambda Role

```json
{
  "Statement": [
    {
      "Sid": "S3BucketAccess",
      "Effect": "Allow",
      "Action": [
        "s3:GetObject", "s3:PutObject", "s3:ListBucket",
        "s3:DeleteObject", "s3:DeleteObjects", "s3:ListObjects",
        "s3:ListObjectsV2", "s3:GetBucketLocation",
        "s3:GetObjectVersion", "s3:GetBucketPolicy"
      ],
      "Resource": ["arn:aws:s3:::${BucketName}", "arn:aws:s3:::${BucketName}/*"]
    },
    {
      "Sid": "BedrockModelInvocation",
      "Effect": "Allow",
      "Action": ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
      "Resource": [
        "arn:aws:bedrock:${Region}::foundation-model/us.amazon.nova-lite-v1:0",
        "arn:aws:bedrock:${Region}::foundation-model/amazon.nova-lite-v1:0",
        "arn:aws:bedrock:${Region}::foundation-model/us.amazon.nova-pro-v1:0",
        "arn:aws:bedrock:${Region}::foundation-model/amazon.nova-pro-v1:0"
      ]
    },
    {
      "Sid": "BedrockDataAutomation",
      "Effect": "Allow",
      "Action": [
        "bedrock:InvokeDataAutomationAsync",
        "bedrock:GetDataAutomationStatus",
        "bedrock:GetDataAutomationProject"
      ],
      "Resource": [
        "${BdaProjectArn}",
        "arn:aws:bedrock:${Region}:${AccountId}:data-automation-invocation/*"
      ]
    },
    {
      "Sid": "BedrockDataAutomationProfile",
      "Effect": "Allow",
      "Action": ["bedrock:InvokeDataAutomationAsync"],
      "Resource": "arn:aws:bedrock:*:${AccountId}:data-automation-profile/*"
    },
    {
      "Sid": "CloudWatchLogs",
      "Effect": "Allow",
      "Action": ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"],
      "Resource": "arn:aws:logs:${Region}:${AccountId}:log-group:/aws/lambda/Pdf2HtmlPipeline:*"
    }
  ]
}
```

> The Lambda role also receives the `AWSLambdaBasicExecutionRole` AWS managed policy.

---

## Security Considerations

### Principle of Least Privilege
- Runtime roles use scoped actions and resource ARNs wherever AWS supports them.
- Deployment (CodeBuild) policies use broader wildcards (`s3:*`, `lambda:*`, etc.) because CDK needs to create, update, and delete resources. These are scoped to specific resource name patterns.

### Services Without Resource-Level Permissions
These actions require `Resource: "*"`:
- `cloudwatch:PutMetricData`
- `comprehend:DetectDominantLanguage`
- `ecr:GetAuthorizationToken`
- `sts:GetCallerIdentity`
- EC2 VPC-related describe operations
- ECS cluster and task definition operations
- Bedrock Data Automation project management actions

### Sensitive Data Protection
- Adobe API credentials stored in AWS Secrets Manager at `/myapp/client_credentials`
- All S3 buckets use server-side encryption (SSE-S3)
- VPC isolates ECS tasks in private subnets (PDF-to-PDF)
- IAM roles scoped to specific resource patterns

---

## Troubleshooting Permission Issues

### Common Errors

1. **CDK Bootstrap Failures** — Ensure CloudFormation and S3 permissions for `cdk-*` resources
2. **ECR Push Failures** — Verify ECR repository permissions and `ecr:GetAuthorizationToken`
3. **Lambda Deployment Failures** — Check Lambda and IAM role creation permissions
4. **Step Function Execution Failures** — Verify Step Functions and ECS permissions
5. **Bedrock Access Denied** — Ensure model access is enabled in the console and IAM policy includes correct model ARNs
6. **BDA Project Creation Failures** — Verify `bedrock:CreateDataAutomationProject` in the pdf2html policy

### Permission Validation
```bash
aws sts get-caller-identity
aws iam get-user
aws bedrock list-foundation-models --region your-region
```

### Model ARN Formats
- Foundation models: `arn:aws:bedrock:${Region}::foundation-model/${ModelId}`
- Data automation projects: `arn:aws:bedrock:${Region}:${AccountId}:data-automation-project/${ProjectId}`
- Data automation invocations: `arn:aws:bedrock:${Region}:${AccountId}:data-automation-invocation/${JobId}`
- Data automation profiles: `arn:aws:bedrock:${Region}:${AccountId}:data-automation-profile/${ProfileId}`
