# PDF_Accessibility_fork — Deploy & Redeploy Guide

> Use this when updating the **PDF-to-PDF** stack on AWS after code changes (including channels worksheet migration).  
> **Related:** [CHANNELS_WORKSHEET_A11Y_PLAN.md](./CHANNELS_WORKSHEET_A11Y_PLAN.md)

---

## Quick answer: do I need to uninstall first?

| Situation                                                | Action                                                       |
| -------------------------------------------------------- | ------------------------------------------------------------ |
| Adding migration script, Lambdas, CodeBuild project, IAM | **`cdk deploy`** — incremental update                        |
| Changing ECS Docker images (Adobe / alt-text containers) | **`cdk deploy`** — rebuilds assets                           |
| Stack broken / CloudFormation drift / clean dev slate    | **`cdk destroy`** then redeploy (optional)                   |
| Re-run `./deploy.sh` from upstream GitHub                | **Does not deploy your fork** unless you change `GITHUB_URL` |

**Default: use `cdk deploy` from this fork. Full uninstall is rarely required.**

---

## Prerequisites

- AWS account with existing **PDFAccessibility** stack (dev: account `264230611910`, region `us-east-1` typical)
- Adobe credentials in Secrets Manager: `/myapp/client_credentials`
- Tools: Python 3.12+, Node.js 18+, AWS CLI, AWS CDK, **Docker** (Colima or Docker Desktop — for Lambda/ECS image builds)
- IAM permissions to deploy CDK stack and (for migration) read/write `channels-data-dev`

See also: [MANUAL_DEPLOYMENT.md](./MANUAL_DEPLOYMENT.md)

---

## Deploy / update from this fork (recommended)

Run from your **local clone** of `PDF_Accessibility_fork` (not upstream `PDF_Accessibility` unless intentional).

```bash
cd /path/to/PDF_Accessibility_fork

# Colima + CDK (recommended on macOS)
colima start
./scripts/cdk-deploy.sh              # deploy PDFAccessibility
./scripts/cdk-deploy.sh diff         # preview changes

# Manual equivalent:
python3 -m venv .venv-cdk && source .venv-cdk/bin/activate
pip install -r requirements.txt
npm install -g aws-cdk
docker context use colima            # see scripts/env.cdk.example
export AWS_DEFAULT_REGION=us-east-1
export BUILDX_NO_DEFAULT_ATTESTATIONS=1
cdk deploy PDFAccessibility --app "python3 app.py" --require-approval never
```

### Colima troubleshooting

| Symptom                                    | Fix                                                   |
| ------------------------------------------ | ----------------------------------------------------- |
| `command not found: docker`                | `brew install docker && brew link --overwrite docker` |
| `spawnSync docker ENOENT` (CDK)            | Docker CLI not on PATH — link brew docker             |
| `Cannot connect to the Docker daemon`      | `colima start` then `docker context use colima`       |
| `DOCKER_HOST overrides the active context` | `unset DOCKER_HOST` — use context instead             |
| Colima socket missing                      | `colima stop && colima start`                         |

Socket path: `~/.colima/default/docker.sock`

### After deploy

1. Confirm stack status: `aws cloudformation describe-stacks --stack-name PDFAccessibility --query 'Stacks[0].StackStatus'`
2. Confirm Step Function exists in console
3. If migration CodeBuild project was added: verify `channels-worksheet-a11y-migrate` in CodeBuild console
4. Smoke test legacy path: upload a PDF to a11y bucket `pdf/test.pdf` → check `result/COMPLIANT_*`

---

## First-time deploy (no existing stack)

If nothing is deployed yet:

**Option A — Manual CDK (uses this repo directly):**

1. Create `client_credentials.json` and upload to Secrets Manager (see [MANUAL_DEPLOYMENT.md](./MANUAL_DEPLOYMENT.md) steps 4–5)
2. `cdk bootstrap` (once per account/region)
3. `cdk deploy`

**Option B — `deploy.sh` (CodeBuild pulls from GitHub):**

```bash
chmod +x deploy.sh
./deploy.sh
# Choose 1) PDF-to-PDF
```

**Important:** `deploy.sh` defaults to `GITHUB_URL=https://github.com/AlexandrLi/PDF_Accessibility.git`.  
To deploy **this fork**, either:

- Edit `deploy.sh` line `GITHUB_URL` to your fork URL + branch, **or**
- Prefer **Option A** (manual `cdk deploy` from fork)

---

## Full uninstall (optional — clean slate)

Only when you intentionally want to tear down the CDK stack.

### 1. Destroy stack

```bash
cd /path/to/PDF_Accessibility_fork
source .venv/bin/activate
export AWS_DEFAULT_REGION=us-east-1

cdk destroy PDFAccessibility
# Confirm when prompted
```

### 2. What is removed vs retained

| Resource                                              | On `cdk destroy`                                                 |
| ----------------------------------------------------- | ---------------------------------------------------------------- |
| Lambdas, Step Functions, ECS, VPC, NAT                | **Deleted**                                                      |
| CloudWatch log groups (most)                          | **Deleted** (some DESTROY policy)                                |
| S3 a11y bucket                                        | **RETAINED** (`RemovalPolicy.RETAIN` in `app.py`) — data remains |
| Secrets Manager `/myapp/client_credentials`           | **Kept** (not owned by stack)                                    |
| CodeBuild projects `pdfremediation-*`                 | **Kept** — created by `deploy.sh`, not CDK                       |
| Migration CodeBuild `channels-worksheet-a11y-migrate` | Deleted **only if** defined in CDK                               |

### 3. Optional manual cleanup

```bash
# List a11y buckets
aws s3 ls | grep -i pdfaccessibility

# Empty retained bucket (optional — destroys processed PDFs)
# aws s3 rm s3://<bucket-name> --recursive

# List old deploy CodeBuild projects
aws codebuild list-projects --query 'projects[?starts_with(@, `pdfremediation`)]'

# Delete old deploy project (optional)
# aws codebuild delete-project --name pdfremediation-20260416090019
```

### 4. Redeploy after destroy

Follow [Deploy / update from this fork](#deploy--update-from-this-fork-recommended) or first-time deploy steps.  
Adobe secret usually still exists — skip recreate if present:

```bash
aws secretsmanager describe-secret --secret-id /myapp/client_credentials
```

---

## Deploying channels migration components

After implementing migration code, deploy infrastructure **once**:

| Component                                         | How it ships                         |
| ------------------------------------------------- | ------------------------------------ |
| Changes to existing Lambdas / Step Function / ECS | `cdk deploy`                         |
| `assemble-chapter` / IAM for channels S3          | `cdk deploy`                         |
| CodeBuild `channels-worksheet-a11y-migrate`       | `cdk deploy` or extend `deploy.sh`   |
| `buildspec-migrate.yml` + migration script        | Git only — CodeBuild reads from repo |

**Order:**

1. Merge migration code to branch used by CodeBuild source
2. `cdk deploy` from fork
3. Create/update CodeBuild migration project (if not in CDK yet)
4. Pilot: CodeBuild dry-run → one chapter → full course ([plan §2.9](./CHANNELS_WORKSHEET_A11Y_PLAN.md#29-aws-execution-codebuild))

Running migration **does not** require redeploying the stack each time — only when **code or IAM** changes.

---

## Environment reference (dev)

| Item                                   | Typical value                                    |
| -------------------------------------- | ------------------------------------------------ |
| AWS account                            | `264230611910`                                   |
| Region                                 | `us-east-1`                                      |
| CDK stack                              | `PDFAccessibility`                               |
| Channels data bucket                   | `channels-data-dev`                              |
| Channels CloudFront (generate-pdf dev) | `E27O7BO97BHXFO`                                 |
| Adobe secret                           | `/myapp/client_credentials`                      |
| A11y S3 bucket                         | `pdfaccessibilitybucket1-*` (CDK-generated name) |

---

## Troubleshooting

| Problem                               | Fix                                                                  |
| ------------------------------------- | -------------------------------------------------------------------- |
| `cdk deploy` fails on Docker/ECR      | See [TROUBLESHOOTING_CDK_DEPLOY.md](./TROUBLESHOOTING_CDK_DEPLOY.md) |
| Python not found on Windows           | Set `"app": "python app.py"` in `cdk.json`                           |
| VPC / Elastic IP limit                | Request quota increase (MANUAL_DEPLOYMENT.md)                        |
| Deploy.sh builds old code             | Deploy from fork with `cdk deploy`, or fix `GITHUB_URL`              |
| Migration can't write channels bucket | Add IAM on CodeBuild role / Lambda task role (plan §11)              |

---

## Related documents

| Doc                                                                  | Purpose                                    |
| -------------------------------------------------------------------- | ------------------------------------------ |
| [CHANNELS_WORKSHEET_A11Y_PLAN.md](./CHANNELS_WORKSHEET_A11Y_PLAN.md) | Migration design, S3 paths, CodeBuild runs |
| [MANUAL_DEPLOYMENT.md](./MANUAL_DEPLOYMENT.md)                       | First-time CDK setup                       |
| [README.md](../README.md)                                            | Upstream one-click deploy overview         |
