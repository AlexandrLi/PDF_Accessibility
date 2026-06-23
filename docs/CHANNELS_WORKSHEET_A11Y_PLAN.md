# Channels Worksheet Accessibility Integration Plan

> **Scope:** Changes limited to `PDF_Accessibility_fork` only.  
> **Goal:** One-time remediation of **existing** topic and chapter worksheets in the channels CDN bucket — replace files in place at the same S3 paths. No changes to other repos.  
> **Run granularity:** Per **course** or per **chapter**.  
> **Execution:** Local CLI (dev) + **AWS CodeBuild** (production migration).  
> **Deploy:** `cdk deploy` from this fork — see [REDEPLOY.md](./REDEPLOY.md). Full uninstall rarely required.  
> **Last updated:** 2026-06-23 (v1: **preview-only** migration; download/chapter via admin Generate worksheets)

> **v1 implementation note:** The migration CLI only remediates `topic_pdfs/{topicId}.pdf`. Sections below that describe fork-owned topic download wrap, chapter assembly, or `--post-wrap-a11y` are **deferred** — kept for reference only.

---

## Table of contents

1. [Executive summary](#1-executive-summary)
2. [Migration mode (primary scope)](#2-migration-mode-primary-scope)
   - [§2.9 AWS CodeBuild](#29-aws-execution-codebuild)
   - [§2.10 Deploy & redeploy](#210-deploy--redeploy)
   - [§2.11 Replace-in-place FAQ](#211-replace-in-place-faq)
   - [§2.12 Two-bucket model](#212-two-bucket-model-critical)
   - [§2.13 Document hierarchy](#213-document-hierarchy-preview--source)
   - [§2.14 Adobe token efficiency](#214-adobe-token-efficiency-default-strategy)
   - [§2.15 Migration summary report](#215-migration-summary-report-schema)
3. [Constraints and non-goals](#3-constraints-and-non-goals)
4. [Current system reference](#4-current-system-reference)
5. [Problem statement](#5-problem-statement)
6. [Target architecture](#6-target-architecture)
7. [S3 contract](#7-s3-contract)
8. [Job metadata schema](#8-job-metadata-schema)
9. [Pipeline stages (detailed)](#9-pipeline-stages-detailed)
10. [Implementation phases](#10-implementation-phases) — includes [remaining backlog](#remaining-implementation-backlog)
11. [CDK / IAM changes](#11-cdk--iam-changes)
12. [New components to build](#12-new-components-to-build)
13. [Step Function changes](#13-step-function-changes)
14. [Batch coordinator design (optional — not used in migration mode)](#14-batch-coordinator-design-optional--not-used-in-migration-mode)
15. [CloudFront invalidation](#15-cloudfront-invalidation)
16. [Course JSON updates (optional — not used in migration mode)](#16-course-json-updates-optional--not-used-in-migration-mode)
17. [Operational runbook](#17-operational-runbook)
18. [Testing checklist](#18-testing-checklist)
19. [Risks and mitigations](#19-risks-and-mitigations)
20. [Open questions](#20-open-questions)

**Related docs:** [REDEPLOY.md](./REDEPLOY.md) · [MANUAL_DEPLOYMENT.md](./MANUAL_DEPLOYMENT.md)

---

## 1. Executive summary

### Key decisions (read first)

| Topic                       | Decision                                                                 |
| --------------------------- | ------------------------------------------------------------------------ |
| **Repos changed**           | `PDF_Accessibility_fork` only                                            |
| **Worksheet scope**         | Existing worksheets only — no new topics/chapters                        |
| **Run frequency**           | One-time migration (not ongoing pipeline)                                |
| **Run granularity**         | Full **course** or single **chapter**                                    |
| **Output (v1)**             | **Preview only** — overwrite `topic_pdfs/{topicId}.pdf`                  |
| **Download / chapter PDFs** | **Admin Generate worksheets** after preview migration (existing process) |
| **Mongo / course JSON**     | **No updates** (flags already set)                                       |
| **`generate-pdf-lambda`**   | **Not invoked by fork** — admin triggers after preview is remediated     |
| **Adobe token strategy**    | **1 Adobe pass per topic preview** — nothing else in v1                  |
| **Local testing**           | `migrate-channels-worksheets-a11y.sh` on laptop                          |
| **Production runs**         | Same script via **CodeBuild**                                            |
| **Infrastructure updates**  | `cdk deploy` from fork — see [REDEPLOY.md](./REDEPLOY.md)                |

**v1 scope (current):** remediate **topic preview only**. Download and chapter worksheets stay out of this fork — rebuild them via **admin → Generate worksheets** after previews are done.

| Layer                | S3 key                                  | v1 responsibility                                          |
| -------------------- | --------------------------------------- | ---------------------------------------------------------- |
| **Preview**          | `topic_pdfs/{topicId}.pdf`              | **This fork** — full Adobe + alt-text, overwrite in place  |
| **Topic download**   | `topic_pdfs/{title}_{topicId}.pdf`      | **Admin Generate worksheets** (reads remediated preview)   |
| **Chapter download** | `worksheets/{chapterTitle}-{tocId}.pdf` | **Admin Generate worksheets** (merges remediated previews) |

**This plan delivers a one-time migration CLI inside `PDF_Accessibility_fork` that:**

1. Discovers **existing** topic preview PDFs from course JSON + S3.
2. Runs **full a11y remediation on preview only** via the existing Adobe + alt-text pipeline.
3. **Overwrites** `topic_pdfs/{topicId}.pdf` in **channels-data**.
4. Invalidates CloudFront for touched preview paths.
5. **Ops then runs admin Generate worksheets** to refresh download + chapter PDFs from the new previews.

Runs at **course** or **chapter** granularity (chapter = filter which topic previews to process). Existing generic a11y uploads to `pdf/*.pdf` remain **unchanged**.

> **Implementation priority:** §2 (migration mode) is the primary delivery. §6–§14 describe supporting technical detail; DynamoDB, ongoing S3 triggers, and Mongo/JSON updates are **out of scope** for migration mode.

---

## 2. Migration mode (primary scope)

### 2.1 What “one-time migration” means

| Assumption                                    | Implication                                                                          |
| --------------------------------------------- | ------------------------------------------------------------------------------------ |
| Only **existing** worksheets                  | Skip topics/chapters with no PDF in S3 and no `pdfAvailable` / `chapterPdfUrls`      |
| Run once per course or chapter                | No ongoing pipeline, no DynamoDB job table required                                  |
| Same S3 paths                                 | Overwrite in place — web app unchanged                                               |
| Flags already in Mongo                        | **No Mongo or course JSON updates**                                                  |
| No admin “Generate worksheets” after previews | **Run Generate worksheets after** preview migration to refresh download/chapter PDFs |

### 2.2 Run scopes

Two supported entry points for the migration CLI:

#### Scope A — Full course

```bash
./scripts/migrate-channels-worksheets-a11y.sh \
  --course-id general-chemistry \
  --env dev
```

**Discovers:**

- All topics in course JSON where `pdfAvailable === true` **and** `topic_pdfs/{topicId}.pdf` exists in S3
- All chapters where `chapterPdfUrls[chapterId]` is set **and** (optionally) `worksheets/{chapterTitle}-{tocId}.pdf` exists

**Processes:**

1. Remediate every in-scope **preview** → overwrite `topic_pdfs/{topicId}.pdf`
2. Invalidate CDN for touched preview paths
3. **Then (ops):** admin Generate worksheets for the course

#### Scope B — Single chapter

```bash
./scripts/migrate-channels-worksheets-a11y.sh \
  --course-id general-chemistry \
  --toc-id default-toc-id \
  --chapter-id ch-04 \
  --env dev
```

**Discovers:**

- Topics listed under that chapter in `course.tocs[tocId].chapters[].topics` that have existing `{topicId}.pdf`
- That chapter only if `chapterPdfUrls[chapterId]` is set

**Processes:**

1. Remediate **only** those topic previews → overwrite `topic_pdfs/{topicId}.pdf`
2. Invalidate CDN for those preview paths
3. **Then (ops):** admin Generate worksheets (at least for this course/chapter)

**Does not touch:** other chapters’ worksheet PDFs or topics in other chapters.

#### Scope resolution (CLI logic)

```
loadCourseJson(courseId)

if --chapter-id provided:
  require --toc-id
  chapter = findChapter(course, tocId, chapterId)
  topics = chapter.topics filtered by existing topic_pdfs/{topicId}.pdf
  chapters = [chapter] if chapterPdfUrls[chapterId] else []
else:
  topics = all course topics with pdfAvailable + S3 object exists
  chapters = all chapters with chapterPdfUrls[chapterId] set

if topics.empty and chapters.empty:
  exit with "nothing to migrate"
```

### 2.3 Files written per run (replace in place)

| Scope       | `{topicId}.pdf` (preview — remediated) | `{title}_{topicId}.pdf` (wrapped) | `worksheets/{chapter}-{toc}.pdf` (merged + wrapped) |
| ----------- | -------------------------------------- | --------------------------------- | --------------------------------------------------- |
| **Course**  | All in-scope topics                    | All in-scope topics               | All in-scope chapters                               |
| **Chapter** | Topics in that chapter only            | Same                              | That chapter only                                   |

**Mongo:** not updated. **Regenerate:** not invoked. **Content a11y:** preview only; download/chapter are derived.

### 2.4 Simplified migration flow

```mermaid
flowchart TD
    A[CLI: course OR chapter scope] --> B[Load course JSON + verify preview PDFs exist]
    B --> C[For each in-scope topic]
    C --> D[Copy preview from channels → a11y bucket]
    D --> E[Full Adobe remediation on preview content]
    E --> F[Overwrite channels topic_pdfs/{topicId}.pdf]
    F --> G[Wrap preview → overwrite topic_pdfs/{title}_{topicId}.pdf]
    G --> H{More topics?}
    H -->|yes| C
    H -->|no| I[For each in-scope chapter in run]
    I --> J[Merge remediated preview PDFs in TOC order + references]
    J --> K[Apply chapter header/footer wrap]
    K --> L[Overwrite channels worksheets/{chapter}-{toc}.pdf]
    L --> M{More chapters?}
    M -->|yes| I
    M -->|no| N[Invalidate CDN for touched paths only]
    N --> O[Print summary report]

    K -. optional .-> K2["--post-wrap-a11y: Adobe on chapter only"]
    K2 -.-> L
```

### 2.5 Chapter scope: incremental course migration

Courses can be migrated **chapter by chapter** over multiple runs:

| Run                 | Effect                                                    |
| ------------------- | --------------------------------------------------------- |
| Chapter A           | Only chapter A worksheet + its topics updated             |
| Chapter B (later)   | Only chapter B + its topics; chapter A unchanged          |
| Full course (later) | Idempotent re-run on all remaining/failed items if needed |

**Recommendation:** Prefer **chapter-scoped** runs for large courses (easier rollback, smaller blast radius, shorter runs).

### 2.6 What migration mode drops from the full plan

| Component                          | Migration mode                                                      |
| ---------------------------------- | ------------------------------------------------------------------- |
| DynamoDB batch coordinator         | **Skip** — CLI loops synchronously or polls Step Function per topic |
| Ongoing `pdf/channels/` S3 trigger | **Skip** — CLI copies sources into a11y bucket per job              |
| Mongo / course JSON updates        | **Skip** — existing flags only                                      |
| Invoke `generateCoursePdf`         | **Never**                                                           |
| Re-process / single-topic API      | **Skip** — use `--chapter-id` or full course re-run                 |

### 2.7 CLI flags (reference)

| Flag                      | Required         | Description                                                              |
| ------------------------- | ---------------- | ------------------------------------------------------------------------ |
| `--course-id`             | Yes              | Course identifier                                                        |
| `--env`                   | Yes              | `dev` / `prod` → selects channels bucket                                 |
| `--toc-id`                | If chapter scope | TOC containing the chapter                                               |
| `--chapter-id`            | No               | If set → chapter scope; if omitted → full course                         |
| `--dry-run`               | No               | List in-scope topics/chapters and S3 keys only                           |
| `--skip-chapter-assembly` | No               | Topic-only pass (rare; chapter PDF stays stale)                          |
| `--skip-cdn-invalidation` | No               | For testing                                                              |
| `--post-wrap-a11y`        | No               | Run Adobe on chapter assembly after wrap (extra tokens; default **off**) |

### 2.8 Exit summary (CLI output)

After each run, print:

```
Run scope: course | chapter (ch-04)
Topics remediated: 12 / 12
Chapters assembled: 1 / 1  (or 8 / 8 for course scope)
Paths overwritten: [list]
CDN invalidation: requested | skipped
Audit failures: 0
```

### 2.9 AWS execution (CodeBuild)

Local CLI is for **dev, dry-run, and debugging**. Production migration runs on **AWS CodeBuild** using the **same script** — no duplicate logic.

#### Why CodeBuild

| Benefit       | Detail                                                      |
| ------------- | ----------------------------------------------------------- |
| Simple        | One project, one buildspec, env vars for scope              |
| Same as local | Invokes `migrate-channels-worksheets-a11y.sh`               |
| Long runs     | Up to 8 hours (enough for full course)                      |
| IAM role      | No laptop credentials; auditable                            |
| Logs          | CloudWatch `/aws/codebuild/channels-worksheet-a11y-migrate` |
| Already used  | Aligns with existing `deploy.sh` CodeBuild pattern          |

#### Architecture

```
Operator
  → aws codebuild start-build (env: COURSE_ID, TOC_ID?, CHAPTER_ID?, ENV)
    → CodeBuild container
      → migrate-channels-worksheets-a11y.sh
        → S3 read/write (channels + a11y buckets)
        → Step Functions (existing per-topic remediation)
        → assemble chapter (merge previews + wrap; optional --post-wrap-a11y)
        → CloudFront invalidation
    → CloudWatch logs + build status
```

#### CodeBuild project

| Setting      | Value                                                       |
| ------------ | ----------------------------------------------------------- |
| Project name | `channels-worksheet-a11y-migrate`                           |
| Buildspec    | `buildspec-migrate.yml` (repo root)                         |
| Image        | `aws/codebuild/amazonlinux-x86_64-standard:5.0`             |
| Compute      | `BUILD_GENERAL1_MEDIUM` (or `LARGE` for big courses)        |
| Timeout      | 480 minutes (8 hours)                                       |
| Source       | Same repo as `PDF_Accessibility_fork` (GitHub / CodeCommit) |

#### Environment variables (set per build)

| Variable                | Required      | Example             | Description                                   |
| ----------------------- | ------------- | ------------------- | --------------------------------------------- |
| `COURSE_ID`             | Yes           | `general-chemistry` | Course to migrate                             |
| `ENV`                   | Yes           | `dev`               | Selects `channels-data-dev` vs prod           |
| `TOC_ID`                | Chapter scope | `default-toc`       | Required when `CHAPTER_ID` set                |
| `CHAPTER_ID`            | No            | `ch-04`             | Omit for full course scope                    |
| `DRY_RUN`               | No            | `true`              | List scope only, no writes                    |
| `SKIP_CDN_INVALIDATION` | No            | `false`             | For pre-prod testing                          |
| `POST_WRAP_A11Y`        | No            | `false`             | Adobe on chapter after wrap (default **off**) |

#### `buildspec-migrate.yml` (reference)

```yaml
version: 0.2

phases:
  install:
    runtime-versions:
      python: 3.12
    commands:
      - pip install -r requirements.txt --quiet

  pre_build:
    commands:
      - echo "Scope course=$COURSE_ID toc=$TOC_ID chapter=$CHAPTER_ID env=$ENV"
      - |
        ARGS="--course-id $COURSE_ID --env $ENV"
        if [ -n "$TOC_ID" ] && [ -n "$CHAPTER_ID" ]; then
          ARGS="$ARGS --toc-id $TOC_ID --chapter-id $CHAPTER_ID"
        fi
        if [ "$DRY_RUN" = "true" ]; then ARGS="$ARGS --dry-run"; fi
        if [ "$SKIP_CDN_INVALIDATION" = "true" ]; then ARGS="$ARGS --skip-cdn-invalidation"; fi
        if [ "$POST_WRAP_A11Y" = "true" ]; then ARGS="$ARGS --post-wrap-a11y"; fi
        export MIGRATE_ARGS="$ARGS"

  build:
    commands:
      - ./scripts/migrate-channels-worksheets-a11y.sh $MIGRATE_ARGS

  post_build:
    commands:
      - echo "Migration build finished"

artifacts:
  files:
    - "reports/migrate-*.json"
  discard-paths: yes
```

The migration script writes a JSON summary to `reports/migrate-{timestamp}.json` for build artifacts.

#### IAM (CodeBuild service role)

Attach to `channels-worksheet-a11y-migrate-role`:

```yaml
# channels-data bucket (env-specific — use parameter or two statements)
- s3:GetObject, s3:PutObject, s3:HeadObject
  on arn:aws:s3:::channels-data-dev/courses/*
  on arn:aws:s3:::channels-data-prod/courses/* # when prod enabled

# a11y processing bucket
- s3:GetObject, s3:PutObject, s3:ListBucket
  on arn:aws:s3:::pdfaccessibility-*/*

# existing remediation Step Function
- states:StartExecution, states:DescribeExecution
  on remediation state machine ARN

# CloudFront
- cloudfront:CreateInvalidation
  on channels distribution ARN

# Logs (default CodeBuild)
- logs:CreateLogGroup, logs:CreateLogStream, logs:PutLogEvents
```

#### How to start a run

**Full course (dev):**

```bash
aws codebuild start-build \
  --project-name channels-worksheet-a11y-migrate \
  --environment-variables-override \
    name=COURSE_ID,value=general-chemistry,type=PLAINTEXT \
    name=ENV,value=dev,type=PLAINTEXT
```

**Single chapter:**

```bash
aws codebuild start-build \
  --project-name channels-worksheet-a11y-migrate \
  --environment-variables-override \
    name=COURSE_ID,value=general-chemistry,type=PLAINTEXT \
    name=TOC_ID,value=default-toc-id,type=PLAINTEXT \
    name=CHAPTER_ID,value=ch-04,type=PLAINTEXT \
    name=ENV,value=dev,type=PLAINTEXT
```

**Dry run:**

```bash
aws codebuild start-build \
  --project-name channels-worksheet-a11y-migrate \
  --environment-variables-override \
    name=COURSE_ID,value=general-chemistry,type=PLAINTEXT \
    name=CHAPTER_ID,value=ch-04,type=PLAINTEXT \
    name=TOC_ID,value=default-toc-id,type=PLAINTEXT \
    name=ENV,value=dev,type=PLAINTEXT \
    name=DRY_RUN,value=true,type=PLAINTEXT
```

**Monitor:**

```bash
aws codebuild batch-get-builds --ids <build-id>
# Logs: CloudWatch → /aws/codebuild/channels-worksheet-a11y-migrate
```

#### Local vs CodeBuild

|             | Local CLI                             | CodeBuild                    |
| ----------- | ------------------------------------- | ---------------------------- |
| When        | Dev, debugging, dry-run               | Prod migration, long courses |
| Credentials | Developer SSO                         | Project IAM role             |
| Script      | `migrate-channels-worksheets-a11y.sh` | **Same script**              |
| Parameters  | CLI flags                             | Env vars → same flags        |

#### CDK / deploy placement

Add CodeBuild project in `PDF_Accessibility_fork` via:

- **Option A (simplest):** extend `deploy.sh` with `create-migrate-codebuild-project` function (mirrors existing project creation), or
- **Option B:** add to `app.py` CDK stack as `aws_codebuild.Project`

Recommend **Option A** first to match repo conventions; move to CDK later if desired.

#### Implementation checklist (CodeBuild)

- [x] Add `buildspec-migrate.yml` to repo root
- [ ] Add `scripts/migrate-channels-worksheets-a11y.sh`
- [ ] Add `scripts/lib/channels_paths.py` (path encoding, scope discovery)
- [ ] Add `lib/wrap_topic_download.py` (preview → download wrap)
- [ ] Add `lib/assemble_chapter.py` (merge previews + wrap; Adobe only if `--post-wrap-a11y`)
- [ ] Create CodeBuild project + IAM role (deploy script or CDK)
- [ ] Document env vars in README / §17 runbook
- [ ] Pilot: dry-run build → single chapter build → full course build

### 2.10 Deploy & redeploy

Infrastructure for the a11y pipeline and migration tooling lives in the **PDFAccessibility** CDK stack (`app.py`).

| Task                                                  | Command / doc                                  |
| ----------------------------------------------------- | ---------------------------------------------- |
| Update Lambdas, Step Function, ECS, new migration IAM | `cdk deploy` from **this fork**                |
| First-time or broken stack                            | Optional `cdk destroy` then `cdk deploy`       |
| Full uninstall guide                                  | [REDEPLOY.md](./REDEPLOY.md)                   |
| First-time secrets / Adobe setup                      | [MANUAL_DEPLOYMENT.md](./MANUAL_DEPLOYMENT.md) |

**Do not assume `./deploy.sh` deploys the fork** — it pulls from `GITHUB_URL` (default upstream). Use `cdk deploy` locally or point CodeBuild source at your fork branch.

**After code changes:**

1. `cdk diff` → `cdk deploy`
2. If CodeBuild migration project is in CDK, it updates automatically
3. Run migration via CodeBuild (§2.9) — **no redeploy needed per migration run**

### 2.11 Replace-in-place FAQ

| Question                                    | Answer                                                                            |
| ------------------------------------------- | --------------------------------------------------------------------------------- |
| Replace S3 files at same paths — enough?    | **Yes**, if you overwrite **all three** keys per topic/chapter (see §2.3)         |
| Only replace `{topicId}.pdf`?               | **No** — preview updates; topic + chapter downloads stay stale                    |
| Update Mongo?                               | **No** — existing `pdfAvailable` / `chapterPdfUrls` already set                   |
| Regenerate via admin “Generate worksheets”? | **No** — destroys a11y tags; overwrites your files                                |
| Invalidate CloudFront?                      | **Yes** — required or users may see cached old PDFs                               |
| Who writes chapter PDF?                     | **Migration script** — merge previews + wrap; not `generate-pdf-lambda`           |
| Which file gets full Adobe content pass?    | **Preview `{topicId}.pdf` only** — download/chapter regen without Adobe (default) |
| Run Adobe on download or chapter?           | **No (default)** — use `--post-wrap-a11y` only if chapter audit fails in pilot    |
| Replace only — skip chapter assembly?       | **No** — chapter file is separate; must overwrite `worksheets/...pdf`             |

### 2.12 Two-bucket model (critical)

Migration touches **two S3 buckets**. Do not conflate them.

| Bucket                                      | Role in migration                                                                       |
| ------------------------------------------- | --------------------------------------------------------------------------------------- |
| **A11y bucket** (`pdfaccessibility-*`)      | Temp processing: copy topic in → run existing Step Function → read `result/COMPLIANT_*` |
| **Channels bucket** (`channels-data-{env}`) | **Source of truth for course JSON + existing PDFs**; **final deliverable** written here |

**Flow:** read source from channels → process in a11y bucket → **write all three CDN keys back to channels** → invalidate CloudFront. The a11y bucket is never the user-facing store.

**Why not “promote + regenerate”?** `generate-pdf-lambda` wraps/merges with `pdf-lib` draw operations that degrade PDF/UA tags. After migration, **never invoke** `generateCoursePdf` — the fork must rebuild download and chapter files from **already-remediated preview PDFs**.

### 2.13 Document hierarchy (preview = source)

This is the core simplification for implementation and ops.

```
                    ┌─────────────────────────────────────┐
                    │  topic_pdfs/{topicId}.pdf           │
                    │  PREVIEW — real worksheet content   │
                    │  ★ ONLY file with full Adobe a11y ★  │
                    └──────────────┬──────────────────────┘
                                   │
              ┌────────────────────┼────────────────────┐
              │ wrap (cover,       │ merge previews     │
              │  header, footer,   │ in TOC order +     │
              │  logo, refs)       │ chapter wrap       │
              ▼                    ▼                    │
   topic_pdfs/{title}_{topicId}.pdf    worksheets/{chapter}-{toc}.pdf
   TOPIC DOWNLOAD (derived)            CHAPTER DOWNLOAD (derived)
```

| PDF                                    | Input                                                     | A11y work                                                                      |
| -------------------------------------- | --------------------------------------------------------- | ------------------------------------------------------------------------------ |
| Preview `{topicId}.pdf`                | Ops upload / existing S3                                  | **Full** Adobe autotag + alt-text (existing pipeline)                          |
| Topic download `{title}_{topicId}.pdf` | Remediated preview                                        | **Wrap only** — same content pages; mirrors `generateTopicPdf`                 |
| Chapter worksheet                      | Remediated **preview** PDFs per topic (not download PDFs) | **Merge + wrap only** — mirrors `generateChaptersPdf`; **no Adobe by default** |

**Adobe token rule:** **One Adobe pass per topic** (preview content only). Download and chapter are mechanical regeneration from already-tagged preview pages — same as today’s lambda regen, but sourced from remediated previews instead of invoking `generateCoursePdf`.

**Optional `--post-wrap-a11y`:** If a compliance pilot shows chapter/download PDFs fail audit after wrap-only regen, enable a second Adobe pass on chapter assembly (never on download by default — download pages are copied from tagged preview).

### 2.14 Adobe token efficiency (default strategy)

**Yes — run a11y only on topic preview, then regenerate download and chapter worksheets without Adobe.** This is the recommended default.

| Step                             | Adobe API?                        | Token cost      |
| -------------------------------- | --------------------------------- | --------------- |
| Preview `{topicId}.pdf`          | **Yes** — full autotag + alt-text | 1× per topic    |
| Download `{title}_{topicId}.pdf` | **No** — wrap remediated preview  | 0               |
| Chapter worksheet                | **No** — merge previews + wrap    | 0               |
| Chapter with `--post-wrap-a11y`  | **Yes** — optional assembly pass  | +1× per chapter |

**Example course:** 40 topics, 8 chapters

| Strategy                                                   | Adobe runs |
| ---------------------------------------------------------- | ---------- |
| Preview only (default)                                     | **40**     |
| Preview + post-wrap on every chapter                       | 48         |
| Adobe on all three file types per topic + chapters (avoid) | 88+        |

**Why this is safe enough:**

- Preview is the iframe surface and the **only** file with unique worksheet content (figures, alt text, reading order).
- Download/chapter add cover pages, headers, footers, and logos — decorative layout on **copied** content pages that were already remediated.
- Today’s prod flow already treats download/chapter as regen-from-preview without a separate content a11y pass.

**Known tradeoff:** `pdf-lib`-style wrap may weaken tag structure on derived PDFs vs preview. Mitigation: post-migration **audit-only** check on download/chapter (no auto Adobe); enable `--post-wrap-a11y` for failing chapters only.

### 2.15 Migration summary report schema

Each run writes `reports/migrate-{timestamp}.json` (local or CodeBuild artifact):

```json
{
  "runId": "20260616T143022Z",
  "scope": "chapter",
  "courseId": "general-chemistry",
  "tocId": "default-toc",
  "chapterId": "ch-04",
  "env": "dev",
  "dryRun": false,
  "topics": {
    "expected": 5,
    "remediated": 5,
    "failed": []
  },
  "chapters": {
    "expected": 1,
    "assembled": 1,
    "failed": []
  },
  "pathsOverwritten": [
    "courses/general-chemistry/topic_pdfs/abc123.pdf",
    "courses/general-chemistry/topic_pdfs/Mole%20Concept_abc123.pdf",
    "courses/general-chemistry/worksheets/Stoichiometry-default-toc.pdf"
  ],
  "cdnInvalidation": { "distributionId": "E27O7BO97BHXFO", "paths": ["..."] },
  "auditFailures": 0,
  "durationSeconds": 1234
}
```

---

## 3. Constraints and non-goals

### Hard constraints

| Constraint                                                      | Implication                                                                      |
| --------------------------------------------------------------- | -------------------------------------------------------------------------------- |
| Only modify `PDF_Accessibility_fork`                            | All integration logic, promote, assembly, invalidation live here                 |
| Do not break existing a11y flows                                | Generic `pdf/` uploads must behave exactly as today                              |
| Do not change other repos                                       | Cannot fix `generate-pdf-lambda` assembly; must bypass it for final deliverables |
| Chapter worksheets must be regenerated from remediated previews | Fork rebuilds download/chapter; Adobe on preview content only (default)          |

### Non-goals (out of scope for this plan)

- Changing admin UI or “Generate worksheets” button behavior
- Modifying `generate-pdf-lambda` to preserve PDF/UA tags
- MongoDB / assets-service publish pipeline changes
- Creating worksheets for topics/chapters that do not already have them
- Ongoing / scheduled remediation pipeline (one-time migration only)
- Replacing manual topic PDF creation upstream (content still originates as `{topicId}.pdf`)

---

## 4. Current system reference

### 4.1 Web app entry points (`channels-web-channels`)

| User action                | Client fetch                   | CDN key                                                                        |
| -------------------------- | ------------------------------ | ------------------------------------------------------------------------------ |
| Worksheet preview (iframe) | Direct CDN                     | `courses/{courseId}/topic_pdfs/{topicId}.pdf`                                  |
| Download topic worksheet   | Direct CDN (`generated: true`) | `courses/{courseId}/topic_pdfs/{encodeURIComponent(title)}_{topicId}.pdf`      |
| Download chapter worksheet | Direct CDN                     | `courses/{courseId}/worksheets/{encodeURIComponent(chapterTitle)}-{tocId}.pdf` |

Gating:

- Worksheet tab visible when `topic.pdfAvailable === true`
- Chapter download button when `toc.chapterPdfUrls[chapterId]` is set

### 4.2 Admin triggers (`channels-web-channels-admin`)

| Action                        | API                                                          | Effect                             |
| ----------------------------- | ------------------------------------------------------------ | ---------------------------------- |
| Upload manual topic worksheet | `POST _internal/courses/:courseId/topics/:topicId/worksheet` | Writes `{topicId}.pdf` to S3       |
| Generate worksheets (bulk)    | `POST _internal/courses/:courseId/generateWorksheet`         | Invokes `generateCoursePdf` lambda |

**Warning:** Admin “Generate worksheets” **overwrites** topic branded and chapter PDFs using `generate-pdf-lambda`. After this plan is live, ops must **not** run bulk generate on a11y-processed courses.

### 4.3 `generate-pdf-lambda` behavior (read-only reference)

Both handlers read **preview** `{topicId}.pdf` — never the download PDF.

| Handler               | Input (preview)           | Output (derived)                        | What it adds                                                                                    |
| --------------------- | ------------------------- | --------------------------------------- | ----------------------------------------------------------------------------------------------- |
| `generateTopicPdf`    | `{topicId}.pdf`           | `{title}_{topicId}.pdf`                 | Cover pages, topic title header, logo, line, page footer, references                            |
| `generateChaptersPdf` | `{topicId}.pdf` per topic | `worksheets/{chapterTitle}-{tocId}.pdf` | Cover, merge topic pages in order, textbook/chapter headers, logo, footers, combined references |

**A11y concern:** `pdf-lib` `drawText` / `drawImage` / `copyPages` on each page degrades PDF/UA tags on the **output** document even when input preview was tagged.

**Migration implication:** remediate preview once → fork rebuilds both derived outputs from remediated previews (same layout as lambda, without invoking lambda).

### 4.4 `PDF_Accessibility_fork` today

| Stage                                  | Output                                          |
| -------------------------------------- | ----------------------------------------------- |
| Upload to a11y bucket `pdf/{name}.pdf` | Triggers splitter → Step Functions              |
| Per-chunk Adobe autotag + alt-text     | `temp/.../FINAL_*`, `COMPLIANT_*`               |
| Merger                                 | `temp/{basename}/merged_{basename}`             |
| Title generator                        | `result/COMPLIANT_{filename}.pdf`               |
| Post-remediation audit                 | Report JSON in `temp/.../accessability-report/` |

All output stays in the **a11y bucket**. No channels integration today.

---

## 5. Problem statement

| Approach                                                 | Why insufficient                                                         |
| -------------------------------------------------------- | ------------------------------------------------------------------------ |
| Remediate preview only, skip rebuild of download/chapter | Preview accessible; download and chapter CDN keys stay stale or untagged |
| Remediate preview → invoke admin “Generate worksheets”   | Lambda wrap/merge **re-breaks** tags on derived PDFs                     |
| Run full Adobe on all three files independently          | Wastes work — download/chapter have **no unique content** vs preview     |
| S3 replace preview without CDN invalidation              | Users may see cached old PDFs                                            |

**Required outcome:** Full Adobe remediation on **preview only**; fork **regenerates** download and chapter PDFs from remediated previews **without Adobe** (default). Optional `--post-wrap-a11y` for chapters that fail audit.

---

## 6. Target architecture

> **Migration mode (§2):** CLI / CodeBuild orchestrates the flow below. No DynamoDB, no ongoing `pdf/channels/` S3 trigger required.

```
┌─────────────────────────────────────────────────────────────────────────────┐
│  migrate-channels-worksheets-a11y.sh  (local OR CodeBuild — same script)     │
├─────────────────────────────────────────────────────────────────────────────┤
│  1. Read courses/{courseId}/{courseId}.json (scope: course or chapter)      │
│  2. For each in-scope topic — PREVIEW ONLY for content a11y:                │
│       copy topic_pdfs/{topicId}.pdf → a11y bucket → [existing Step Function]│
│       → overwrite channels topic_pdfs/{topicId}.pdf  (remediated preview)   │
│       → wrap remediated preview → topic_pdfs/{title}_{topicId}.pdf          │
│  3. For each in-scope chapter:                                              │
│       merge remediated PREVIEW PDFs (TOC order) + references                │
│       → apply chapter header/footer wrap                                    │
│       → overwrite channels worksheets/{chapterTitle}-{tocId}.pdf            │
│       → (optional) --post-wrap-a11y: Adobe on chapter assembly only         │
│  4. CloudFront invalidation (scoped paths)                                  │
├─────────────────────────────────────────────────────────────────────────────┤
│  EXISTING PATH (unchanged — generic uploads)                                │
│  pdf/{file}.pdf → split → remediate → result/COMPLIANT_*  (a11y bucket only)│
└─────────────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│  channels-data-{env}  — overwrite in place, no Mongo, no regen              │
│  topic_pdfs/{topicId}.pdf          ← preview (★ content a11y source ★)      │
│  topic_pdfs/{title}_{topicId}.pdf  ← derived wrap of preview                │
│  worksheets/{chapter}-{toc}.pdf    ← derived merge of previews + wrap       │
└─────────────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│  channels-web-channels (unchanged) — same CDN URLs                           │
└─────────────────────────────────────────────────────────────────────────────┘
```

### Design principles

1. **Fork-only changes** — no edits to channels, assets-service, or generate-pdf-lambda.
2. **Preview is the content source** — one full Adobe pass per topic on `{topicId}.pdf` only.
3. **Download/chapter are derived** — wrap or merge remediated previews; no separate content remediation.
4. **Fork owns deliverables** — rebuild wraps in fork; never invoke `generateCoursePdf`.
5. **Regen without Adobe (default)** — download/chapter rebuilt from tagged previews; saves Adobe tokens.
6. **Optional `--post-wrap-a11y`** — second Adobe pass on chapter assembly only if audit requires it.
7. **Same URLs** — replace bytes in place; invalidate CDN.
8. **Course or chapter scope** — CLI/CodeBuild env vars select scope.
9. **Legacy a11y path untouched** — generic `pdf/` uploads unchanged.

### Deferred architecture (not used in migration mode)

The diagram below described an optional ongoing `pdf/channels/` trigger + DynamoDB coordinator. **Not building for v1.**

```
┌─ OPTIONAL FUTURE (deferred) ────────────────────────────────────────────────┐
│  pdf/channels/{courseId}/{topicId}.pdf → promote → DynamoDB → assemble      │
└─────────────────────────────────────────────────────────────────────────────┘
```

---

## 7. S3 contract

### 7.1 Buckets

| Bucket                 | Env var                  | Usage                                                    |
| ---------------------- | ------------------------ | -------------------------------------------------------- |
| A11y processing bucket | From CDK / `A11Y_BUCKET` | Temp remediation during migration copy                   |
| Channels data bucket   | `CHANNELS_DATA_BUCKET`   | **Read** course JSON + source PDFs; **write** final PDFs |

| Environment | Channels bucket      |
| ----------- | -------------------- |
| Dev         | `channels-data-dev`  |
| Prod        | `channels-data-prod` |

### 7.2 Input during migration (CLI-driven)

Migration script **reads from channels bucket** and **copies to a11y bucket** for processing — no standing `pdf/channels/` trigger required for v1.

| Step             | Location                                                      |
| ---------------- | ------------------------------------------------------------- |
| Read source      | `channels-data-*/courses/{courseId}/topic_pdfs/{topicId}.pdf` |
| Read course JSON | `channels-data-*/courses/{courseId}/{courseId}.json`          |
| Temp processing  | a11y bucket (existing pipeline)                               |

**Legacy generic input (unchanged):** `pdf/{filename}.pdf` on a11y bucket → no channels writes.

### 7.3 Output (channels bucket — overwrite in place)

| Key                                                               | Written by       | Consumed by      |
| ----------------------------------------------------------------- | ---------------- | ---------------- |
| `courses/{courseId}/topic_pdfs/{topicId}.pdf`                     | Migration script | Preview iframe   |
| `courses/{courseId}/topic_pdfs/{encodedTitle}_{topicId}.pdf`      | Migration script | Topic download   |
| `courses/{courseId}/worksheets/{encodedChapterTitle}-{tocId}.pdf` | Migration script | Chapter download |

**Encoding:** `encodeURIComponent(title)` — must match channels-web-channels / generate-pdf-lambda convention.

### 7.4 Job reports (optional)

| Path                               | Purpose                                                |
| ---------------------------------- | ------------------------------------------------------ |
| `reports/migrate-{timestamp}.json` | CLI/CodeBuild summary (local artifact or build output) |

### 7.5 Course JSON — read only

Path: `courses/{courseId}/{courseId}.json` — used for topic titles, chapter order, references, `pdfAvailable`, `chapterPdfUrls`. **Not written** in migration mode (§16).

---

## 8. Job metadata schema

> **Migration mode:** Metadata comes from **course JSON** at runtime (CLI loads `courseId`, `topicId`, `topicTitle`, `tocId`, `chapterId`). The S3 sidecar format below is **optional / deferred** if not using `pdf/channels/` trigger.

### 8.1 In-memory job object (migration script)

```json
{
  "courseId": "general-chemistry",
  "topicId": "abc123",
  "topicTitle": "Mole Concept",
  "tocId": "toc-default",
  "chapterId": "ch-04",
  "chapterTitle": "Stoichiometry",
  "skipTitleLlm": true
}
```

Built by CLI from course JSON — not required on uploaded S3 objects for v1.

### 8.2 Optional S3 sidecar (deferred — ongoing trigger path)

`pdf/channels/{courseId}/{topicId}.job.json`:

```json
{
  "jobType": "channels-worksheet",
  "runId": "20260616T120000Z",
  "courseId": "general-chemistry",
  "topicId": "abc123",
  "topicTitle": "Mole Concept",
  "tocId": "toc-default",
  "chapterId": "ch-04",
  "chapterTitle": "Stoichiometry",
  "skipTitleLlm": true,
  "writeGeneratedTopicPdf": true,
  "updateCourseJson": false
}
```

### 8.3 Step Function payload (per topic)

When migration script triggers existing remediation for one topic, pass context for title-generator skip:

```json
{
  "channelsJob": {
    "skipTitleLlm": true,
    "topicTitle": "Mole Concept"
  }
}
```

Migration script reads remediated output from a11y bucket and writes to channels — promote step is **in the script**, not a separate Lambda trigger (v1).

---

## 9. Pipeline stages (detailed)

> Orchestrator: **migration script** (local or CodeBuild). Per topic: Adobe on preview → wrap download (no Adobe). Per chapter: merge previews → wrap (no Adobe unless `--post-wrap-a11y`).

### Stage A — Preview remediation (existing pipeline) ★ content a11y ★

For each in-scope topic:

1. **Head** `channels.../topic_pdfs/{topicId}.pdf` — skip if missing
2. **Copy preview** to a11y bucket for processing
3. Run existing chain: split → Adobe autotag → alt-text → merge → title → post-audit
4. **Title step:** use `topicTitle` from course JSON (skip Bedrock) when `skipTitleLlm`
5. **Write back** remediated preview → `courses/{courseId}/topic_pdfs/{topicId}.pdf`

This is the **only** stage that consumes Adobe API tokens for content remediation.

### Stage B — Topic download wrap (derived, in migration script)

After preview is remediated:

1. Load remediated `{topicId}.pdf` from channels bucket (or a11y output before promote)
2. **Wrap** mirroring `generateTopicPdf`:
   - Prepend cover pages (`cover_study_prep.pdf`)
   - Copy remediated content pages
   - Append topic references (HTML → PDF if present)
   - Draw topic title header, line, logo, page footers on each page
3. `PUT` → `courses/{courseId}/topic_pdfs/{encodeURIComponent(topicTitle)}_{topicId}.pdf`

**No Adobe** — input content pages are already tagged in preview. Run audit-only if configured.

**Do not invoke `generateCoursePdf` / `generateTopicPdf`.**

### Stage C — Chapter assembly (derived merge + wrap, no Adobe by default)

After **all topics in scope for that chapter** have remediated previews (same run):

1. Load remediated **preview** `{topicId}.pdf` files from channels bucket in **TOC order** (not download PDFs)
2. **Merge + wrap** mirroring `generateChaptersPdf`:
   - Prepend cover pages
   - Copy each topic’s preview pages in chapter order
   - Append combined references section
   - Draw textbook author/title header, chapter title, line, logo, page footers
3. `PUT` → `courses/{courseId}/worksheets/{encodeURIComponent(chapterTitle)}-{tocId}.pdf`
4. **Optional** (only if `--post-wrap-a11y`): run Adobe autotag on assembled chapter — costs +1 Adobe pass per chapter
5. Audit (Adobe checker API if available — audit may use fewer tokens than full autotag; confirm in pilot)
6. Scoped CloudFront invalidation (§15)

### Stage D — Adobe token usage summary

| Output PDF                       | Adobe autotag (default)            | Regen / wrap          |
| -------------------------------- | ---------------------------------- | --------------------- |
| Preview `{topicId}.pdf`          | **Yes** — 1× per topic             | —                     |
| Download `{title}_{topicId}.pdf` | **No**                             | Wrap preview          |
| Chapter worksheet                | **No** (unless `--post-wrap-a11y`) | Merge previews + wrap |

**Default token budget per course:** `N` Adobe runs where `N` = number of in-scope topics. Chapters add **zero** Adobe cost unless flag enabled.

---

## 10. Implementation phases

> Phases below target **migration mode** (§2). Build the CLI first; permanent S3 triggers and DynamoDB are deferred.

### Phase 0 — Discovery & validation (no production writes)

- [ ] Confirm IAM access to `channels-data-dev` from a11y account/role
- [ ] Confirm CloudFront distribution ID(s) for channels CDN
- [ ] Validate course JSON structure for 1–2 pilot courses
- [ ] Test **course scope** and **chapter scope** discovery logic against real JSON
- [ ] Baseline a11y scores: topic source, post-remediation topic, current chapter PDF
- [ ] Verify existing dev stack: `cdk list` → `PDFAccessibility` ([REDEPLOY.md](./REDEPLOY.md))

### Phase 1 — Migration CLI + preview remediation + topic wrap

**Deliverables:**

- [ ] `scripts/migrate-channels-worksheets-a11y.sh` with `--course-id` and `--chapter-id` + `--toc-id`
- [ ] `--dry-run` lists in-scope topics/chapters and target S3 keys
- [ ] Remediate **preview only** → overwrite `{topicId}.pdf`
- [ ] `lib/wrap_topic_download.py` — rebuild `{title}_{topicId}.pdf` from remediated preview (mirror `generateTopicPdf`)
- [ ] Skip Bedrock title when topic title known from course JSON
- [ ] Legacy `pdf/*` a11y path regression test (unchanged)

**Exit criteria:**

- Chapter-scoped run updates only that chapter’s topics (preview + download)
- Course-scoped run updates all existing topic previews + downloads in course
- No Mongo writes, no generate-pdf invoke

### Phase 2 — Chapter assembly (merge previews + wrap)

**Deliverables:**

- [ ] `lib/assemble_chapter.py` — merge remediated previews → chapter wrap → overwrite `worksheets/...pdf` (Adobe only if `--post-wrap-a11y`)
- [ ] Reuse cover/logo/assets from same paths as `generate-pdf-lambda` (bundled in fork or S3)
- [ ] Course scope: loop all chapters with `chapterPdfUrls`
- [ ] Chapter scope: assemble **one** chapter only
- [ ] Scoped CloudFront invalidation (§15)

**Exit criteria:**

- Chapter download PDF passes post-remediation audit
- Other chapters untouched when running with `--chapter-id`

### Phase 3 — CodeBuild + deploy + pilot

**Deliverables:**

- [ ] `buildspec-migrate.yml` (repo root — **done**)
- [ ] CodeBuild project `channels-worksheet-a11y-migrate` + IAM role
- [ ] `cdk deploy` with channels bucket + CloudFront permissions ([REDEPLOY.md](./REDEPLOY.md))
- [ ] Pilot: dry-run build → single chapter → full course (dev)
- [ ] Runbook §17 + REDEPLOY.md

### Phase 4 — Production rollout

**Deliverables:**

- [ ] Prod env vars / bucket permissions on CodeBuild role
- [ ] Chapter-by-chapter prod migration schedule
- [ ] Summary report artifacts per build

### Deferred (not needed for migration mode)

- DynamoDB batch coordinator (§14)
- Ongoing `pdf/channels/` S3 event trigger (§13)
- Course JSON updates (§16)

### Remaining implementation (backlog)

| #   | Component                                           | Status       | Notes                                     |
| --- | --------------------------------------------------- | ------------ | ----------------------------------------- |
| 1   | `scripts/migrate-channels-worksheets-a11y.sh`       | **Done**     | Preview-only orchestrator                 |
| 2   | `scripts/lib/channels_paths.py`                     | **Done**     | Scope discovery                           |
| 3   | `scripts/lib/remediation.py`                        | **Done**     | Adobe pass + Step Function                |
| 4   | `title-generator-lambda` tweak                      | **Done**     | skipTitleLlm (needs cdk deploy)           |
| 5   | CDK deploy (source_pdf_key + channelsJob)           | **Pending**  | Required before live runs                 |
| 6   | `lib/wrap_topic_download.py` / chapter assembly     | **Deferred** | Use admin Generate worksheets instead     |
| 7   | CodeBuild project `channels-worksheet-a11y-migrate` | **Deferred** | Local CLI first; CodeBuild optional later |
| 8   | `buildspec-migrate.yml`                             | **Done**     | Repo root                                 |
| 9   | Pilot on dev (calculus / 917d0a39)                  | **Pending**  | After cdk deploy                          |

## 11. CDK / IAM changes

All in `PDF_Accessibility_fork/app.py` (+ migration script IAM on CodeBuild role). Deploy with `cdk deploy` — see [REDEPLOY.md](./REDEPLOY.md).

### 11.1 New environment variables

| Variable                              | Example             | Used by          |
| ------------------------------------- | ------------------- | ---------------- |
| `CHANNELS_DATA_BUCKET`                | `channels-data-dev` | migration script |
| `CHANNELS_CLOUDFRONT_DISTRIBUTION_ID` | `E27O7BO97BHXFO`    | invalidation     |
| `CHANNELS_ENV`                        | `dev`               | bucket selection |

### 11.2 IAM permissions to add

**Migration CodeBuild role + any Lambdas used for chapter assembly:**

```yaml
# channels-data bucket
- s3:GetObject on courses/{courseId}/{courseId}.json
- s3:HeadObject on courses/{courseId}/topic_pdfs/*
- s3:GetObject on courses/{courseId}/topic_pdfs/*
- s3:PutObject on courses/{courseId}/topic_pdfs/*
- s3:PutObject on courses/{courseId}/worksheets/*

# a11y bucket
- s3:GetObject, s3:PutObject, s3:ListBucket on pdfaccessibility-*/*

# existing remediation Step Function
- states:StartExecution, states:DescribeExecution

# CloudFront
- cloudfront:CreateInvalidation
# Do NOT grant lambda:InvokeFunction on generate-pdf-*
```

### 11.3 title-generator change (fork only)

Skip Bedrock when migration passes `skipTitleLlm` + `topicTitle` (§8.3, §9 Stage A).

---

## 12. New components to build

### 12.1 `scripts/migrate-channels-worksheets-a11y.sh` (primary)

Orchestrates full migration — used locally and by CodeBuild.

| Responsibility   | Details                                                   |
| ---------------- | --------------------------------------------------------- |
| Scope resolution | Course vs chapter (§2.2)                                  |
| Discovery        | Existing **preview** PDFs via course JSON + S3 HeadObject |
| Per-topic loop   | Remediate preview → wrap download                         |
| Per-chapter loop | Merge previews → wrap (optional `--post-wrap-a11y`)       |
| CDN              | Scoped invalidation (§15)                                 |
| Report           | `reports/migrate-{timestamp}.json`                        |

### 12.2 `lib/wrap_topic_download.py`

Rebuild topic download from remediated preview — port logic from `generate-pdf-lambda/src/handlers/generate-topic-pdf.ts`:

| Step          | Mirrors lambda                             |
| ------------- | ------------------------------------------ |
| Cover pages   | `cover_study_prep.pdf`                     |
| Content       | Copy pages from remediated `{topicId}.pdf` |
| References    | `topic.references` via HTML → PDF          |
| Header/footer | Title, line, logo, page numbers            |

### 12.3 `lib/assemble_chapter.py`

Rebuild chapter worksheet from remediated **preview** PDFs — port logic from `generate-pdf-lambda/src/handlers/generate-chapters-pdf.ts`:

| Step  | Mirrors lambda                                           |
| ----- | -------------------------------------------------------- |
| Input | `{topicId}.pdf` per topic in chapter (not download PDFs) |
| Merge | TOC order + combined references                          |
| Wrap  | Textbook author/title, chapter title, logo, footers      |
| A11y  | None by default; Adobe only if `--post-wrap-a11y`        |

### 12.4 CodeBuild + `buildspec-migrate.yml`

See §2.9. **File exists** at repo root.

### 12.5 Shared utilities (`scripts/lib/channels_paths.py`)

- `encode_topic_pdf_filename(topicTitle, topicId)`
- `encode_chapter_pdf_filename(chapterTitle, tocId)`
- `load_course_json(courseId, bucket)`
- `list_migration_scope(course, tocId?, chapterId?)`

### 12.6 Deferred (not v1)

- `promote-topic-lambda` as separate Step Function step
- `channels-batch-coordinator` / DynamoDB (§14)
- Ongoing `pdf/channels/` S3 trigger (§13)

---

## 13. Step Function changes

**Migration mode:** Existing remediation Step Function is invoked **per topic** by the migration script. Minimal changes:

### 13.1 title-generator-lambda

```python
if channels_job and channels_job.get("skipTitleLlm"):
    title = channels_job["topicTitle"]
else:
    title = generate_title(extracted_text, file_name)
```

### 13.2 Optional: wait for execution helper

Migration script should `StartExecution` + poll `DescribeExecution` until `SUCCEEDED` / `FAILED` per topic.

### 13.3 Deferred (§14)

Step Function branch `PromoteTopic → DynamoDB` for ongoing `pdf/channels/` trigger — not v1.

---

## 14. Batch coordinator design (optional — not used in migration mode)

> **Not building for v1.** Migration script loops topics synchronously (or polls Step Function per topic) and assembles chapters in-process. This section documents a possible future ongoing pipeline.

### 14.1 DynamoDB table: `pdf-a11y-channels-jobs`

**Partition key:** `PK = RUN#{runId}`  
**Sort key:** `SK = TOPIC#{courseId}#{topicId}` | `CHAPTER#{courseId}#{chapterId}` | `META`

**Chapter aggregate item:**

```json
{
  "PK": "RUN#20260616T120000Z",
  "SK": "CHAPTER#general-chemistry#ch-04",
  "courseId": "general-chemistry",
  "chapterId": "ch-04",
  "chapterTitle": "Stoichiometry",
  "tocId": "toc-default",
  "expectedTopicIds": ["t1", "t2", "t3"],
  "completedTopicIds": ["t1"],
  "status": "in_progress | ready | assembling | completed | failed",
  "chapterOutputKey": null
}
```

### 14.2 Completion logic

```
On PromoteTopic success:
  1. Mark topic item status = promoted
  2. ADD topicId to chapter.completedTopicIds (SET)
  3. If size(completedTopicIds) == size(expectedTopicIds):
       SET chapter.status = ready
       Invoke assemble-chapter-lambda (async)
```

Use conditional writes to ensure assemble runs exactly once per chapter per run.

---

## 15. CloudFront invalidation

After each migration run, invalidate **only paths touched in that run**.

### Course scope

```
/courses/{courseId}/topic_pdfs/*
/courses/{courseId}/worksheets/*
```

### Chapter scope

```
/courses/{courseId}/topic_pdfs/{topicId}.pdf          # each topic in chapter
/courses/{courseId}/topic_pdfs/{title}_{topicId}.pdf  # each topic in chapter
/courses/{courseId}/worksheets/{chapterTitle}-{tocId}.pdf
```

Do **not** invalidate unrelated chapters when running chapter scope.

Reuse pattern from `generate-pdf-lambda/src/utils/cloudfront.ts` (port to Python in fork).

---

## 16. Course JSON updates (optional — not used in migration mode)

**Enable only after validating publish pipeline.**

When chapter PDF written:

1. GET `courses/{courseId}/{courseId}.json`
2. SET `tocs[tocId].chapterPdfUrls[chapterId] = true`
3. For each promoted topic: ensure `topics[topicId].pdfAvailable = true` (if field exists in JSON)
4. PUT course JSON back

**Risks:**

- Concurrent edits from admin publish
- Schema drift between S3 JSON and Mongo

**Mitigation:**

- Use optimistic locking via `ETag` on S3 PUT
- Gate behind `CHANNELS_COURSE_JSON_ENABLED=true`
- Log diff before write

---

## 17. Operational runbook

### 17.0 Before first migration run

1. Deploy latest fork code: `cdk deploy` ([REDEPLOY.md](./REDEPLOY.md))
2. Confirm CodeBuild project `channels-worksheet-a11y-migrate` exists
3. Confirm CodeBuild role can read/write `channels-data-dev`

### 17.1 Processing previews (one-time)

1. Confirm target topics have existing preview PDFs (`pdfAvailable` + S3 `topic_pdfs/{topicId}.pdf`).
2. Run migration CLI (course or chapter scope).
3. Monitor Step Functions / CloudWatch for failures.
4. Spot-check **preview iframe** in web app.
5. **Run admin Generate worksheets** for the course to rebuild topic download + chapter PDFs from remediated previews.

### 17.2 Processing a single chapter (one-time)

1. Identify `courseId`, `tocId`, `chapterId` from admin or course JSON.
2. Run with `--chapter-id` and `--toc-id`.
3. Verify **only** that chapter’s topics and chapter PDF changed (other chapters unchanged).
4. Use for large courses: migrate chapter-by-chapter over multiple sessions.

### 17.3 Re-running a failed chapter

1. Fix root cause (missing source PDF, audit failure, etc.).
2. Re-run same `--chapter-id` command — overwrites same paths (idempotent).

### 17.4 Local CLI (dev / debugging)

Same script as CodeBuild — use CLI flags instead of env vars:

```bash
# Dry run
./scripts/migrate-channels-worksheets-a11y.sh \
  --course-id general-chemistry \
  --toc-id default-toc \
  --chapter-id ch-04 \
  --env dev \
  --dry-run

# Single chapter
./scripts/migrate-channels-worksheets-a11y.sh \
  --course-id general-chemistry \
  --toc-id default-toc \
  --chapter-id ch-04 \
  --env dev

# Full course
./scripts/migrate-channels-worksheets-a11y.sh \
  --course-id general-chemistry \
  --env dev
```

Requires AWS credentials with channels bucket + Step Function access (developer SSO on dev account `264230611910`).

### 17.5 Running on AWS (CodeBuild)

**Dry run (recommended first):**

```bash
aws codebuild start-build \
  --project-name channels-worksheet-a11y-migrate \
  --environment-variables-override \
    name=COURSE_ID,value=<course-id>,type=PLAINTEXT \
    name=TOC_ID,value=<toc-id>,type=PLAINTEXT \
    name=CHAPTER_ID,value=<chapter-id>,type=PLAINTEXT \
    name=ENV,value=dev,type=PLAINTEXT \
    name=DRY_RUN,value=true,type=PLAINTEXT
```

**Single chapter migration:**

```bash
aws codebuild start-build \
  --project-name channels-worksheet-a11y-migrate \
  --environment-variables-override \
    name=COURSE_ID,value=<course-id>,type=PLAINTEXT \
    name=TOC_ID,value=<toc-id>,type=PLAINTEXT \
    name=CHAPTER_ID,value=<chapter-id>,type=PLAINTEXT \
    name=ENV,value=dev,type=PLAINTEXT
```

**Full course:** omit `TOC_ID` and `CHAPTER_ID`.

Monitor build in CodeBuild console or CloudWatch logs. Download `reports/migrate-*.json` from build artifacts when complete.

### 17.6 What NOT to do

| Action                                               | Why                                                           |
| ---------------------------------------------------- | ------------------------------------------------------------- |
| Expect download/chapter PDFs to update automatically | v1 only writes preview; run admin Generate worksheets after   |
| Skip admin Generate worksheets after preview pass    | Download/chapter PDFs stay stale until regen                  |
| Run migration without CDK deploy (source_pdf_key)    | Pre-remediation Step Function branch may fail on wrong S3 key |

---

## 18. Testing checklist

### 18.1 Regression (legacy path)

- [ ] Upload `pdf/test-document.pdf` → completes → `result/COMPLIANT_test-document.pdf`
- [ ] No writes to channels bucket
- [ ] Pre/post audit reports generated

### 18.2 Migration dry-run

- [ ] `./scripts/migrate-channels-worksheets-a11y.sh --course-id X --dry-run` lists correct topic/chapter keys
- [ ] No S3 writes, no Step Function starts
- [ ] Report written to `reports/migrate-*.json`

### 18.3 Single chapter migration

- [ ] `--chapter-id` run updates only that chapter’s topics + one worksheet
- [ ] Other chapters’ worksheet PDFs unchanged (compare ETag/hash before/after)
- [ ] All three S3 key patterns overwritten for in-scope topics
- [ ] That chapter’s `worksheets/...pdf` updated
- [ ] CloudFront invalidation limited to touched paths
- [ ] Bedrock title **not** called when `skipTitleLlm=true`

### 18.4 Full course migration

- [ ] All chapters in course get updated worksheets
- [ ] Summary report counts match expected topics/chapters
- [ ] Web preview + topic download + chapter download all show remediated content

### 18.5 A11y validation

- [ ] Adobe checker score: topic source vs remediated topic vs chapter final
- [ ] Manual screen reader spot check (NVDA/VoiceOver) on chapter PDF
- [ ] Compare tag tree exists in chapter final (not just pre-merge topics)

### 18.6 Failure modes

- [ ] Missing topic PDF at source → logged, chapter skipped or partial per policy
- [ ] Partial chapter (1 of N topics fails) → chapter not assembled; report lists failures
- [ ] Re-run same scope idempotent (overwrite same keys)

---

## 19. Risks and mitigations

| Risk                                                     | Impact | Mitigation                                                                      |
| -------------------------------------------------------- | ------ | ------------------------------------------------------------------------------- |
| Admin “Generate worksheets” overwrites a11y chapter PDFs | High   | Ops runbook; optional S3 object metadata `a11y-version` for audit               |
| Wrap may weaken tags on derived PDFs vs preview          | Medium | Default: audit-only on download/chapter; `--post-wrap-a11y` per failing chapter |
| Adobe token overrun on large courses                     | Medium | Preview-only default: N topics = N Adobe runs, not N + chapters                 |
| Mongo `chapterPdfUrls` out of sync                       | N/A    | Not updated in migration — flags pre-existing                                   |
| CDN stale cache                                          | Medium | Invalidate both `topic_pdfs/*` and `worksheets/*`                               |
| Course JSON concurrent write                             | Medium | ETag conditional PUT; feature flag                                              |
| Chapter ECS remediation timeout                          | Medium | 900s timeout; chunk chapter if page count exceeds threshold                     |
| Filename encoding mismatch                               | Medium | Shared `encodeTopicPdfFilename` util tested against known courses               |
| Deploy from wrong repo (deploy.sh upstream URL)          | Medium | Use `cdk deploy` from fork — [REDEPLOY.md](./REDEPLOY.md)                       |

---

## 20. Open questions

| #   | Question                                                                       | Owner      | Blocks                        |
| --- | ------------------------------------------------------------------------------ | ---------- | ----------------------------- |
| 1   | Default TOC when course has multiple tocs — use `defaultToc` from course JSON? | Eng        | Chapter scope CLI             |
| 2   | Maximum chapter page count when `--post-wrap-a11y` enabled?                    | Eng        | Optional chapter Adobe sizing |
| 3   | Should failed audit block overwrite or only warn?                              | Compliance | Audit gate config             |
| 4   | Prod rollout: same pipeline for `channels-data-prod`?                          | Eng/Ops    | CDK prod config               |
| 5   | Bundle cover/logo assets in fork vs fetch from shared S3?                      | Eng        | wrap/assemble modules         |

---

## Appendix A — File path quick reference

```
# Migration input (channels bucket — read)
courses/{courseId}/{courseId}.json
courses/{courseId}/topic_pdfs/{topicId}.pdf

# Temp processing (a11y bucket)
pdf/migrate/{courseId}/{topicId}.pdf          # copy target for Step Function trigger
result/COMPLIANT_{topicId}.pdf
temp/{topicId}/...

# Migration output (channels bucket — overwrite in place)
courses/{courseId}/topic_pdfs/{topicId}.pdf                        ← preview (content a11y source)
courses/{courseId}/topic_pdfs/{encodeURIComponent(title)}_{topicId}.pdf  ← download (wrap of preview)
courses/{courseId}/worksheets/{encodeURIComponent(chapterTitle)}-{tocId}.pdf ← chapter (merge of previews + wrap)

# Deferred ongoing trigger (NOT v1 — do not rely on for migration)
pdf/channels/{courseId}/{topicId}.pdf
pdf/channels/{courseId}/{topicId}.job.json
```

## Appendix B — Related repos (read-only, do not modify)

| Repo                          | Role                                                  |
| ----------------------------- | ----------------------------------------------------- |
| `channels-web-channels`       | CDN fetch for preview/download                        |
| `channels-web-channels-admin` | Upload source PDF; generate button (avoid after a11y) |
| `assets-service`              | Worksheet API, S3 upload for manual worksheets        |
| `generate-pdf-lambda`         | Legacy assembly (bypass for final deliverables)       |

## Appendix C — Glossary

| Term                  | Meaning                                                                                    |
| --------------------- | ------------------------------------------------------------------------------------------ |
| Preview PDF           | `topic_pdfs/{topicId}.pdf` — real worksheet content; **only** file with full content a11y  |
| Topic download PDF    | `topic_pdfs/{title}_{topicId}.pdf` — derived wrap of preview (cover, header, footer, logo) |
| Chapter worksheet     | `worksheets/{chapterTitle}-{tocId}.pdf` — derived merge of **preview** PDFs + chapter wrap |
| Derived PDF           | Built from remediated preview(s); no unique content beyond layout/branding                 |
| Post-wrap remediation | Optional `--post-wrap-a11y`: Adobe on chapter assembly after wrap (extra tokens)           |
| Promote               | Write remediated preview from a11y bucket to channels bucket                               |

## Appendix D — Document index

| Document                                                             | Purpose                           |
| -------------------------------------------------------------------- | --------------------------------- |
| [CHANNELS_WORKSHEET_A11Y_PLAN.md](./CHANNELS_WORKSHEET_A11Y_PLAN.md) | This plan — migration design      |
| [REDEPLOY.md](./REDEPLOY.md)                                         | Deploy, update, optional destroy  |
| [MANUAL_DEPLOYMENT.md](./MANUAL_DEPLOYMENT.md)                       | First-time CDK + Adobe secrets    |
| [buildspec-migrate.yml](../buildspec-migrate.yml)                    | CodeBuild spec for migration runs |

---

_End of plan._
