# Topic preview scope (canonical)

> **Last updated:** 2026-07-14  
> This document is the source of truth for what `PDF_Accessibility_fork` owns. If other docs disagree, this wins.

## What this repo owns

**Only** topic preview PDFs:

```
s3://{channels-bucket}/courses/{courseId}/topic_pdfs/{topicId}.pdf
```

Pipeline per preview:

1. Adobe remediation + alt-text (Step Functions) — `migrate_channels_worksheets.py`
2. Post-remediation sweeps — `sweep_topic_previews.py` or the same sweeps inside migration

Success criterion: **Acrobat Accessibility Checker passes on the preview file itself** (not on derived worksheets).

## What this repo does not own

| Artifact | S3 path | Owner |
|----------|---------|--------|
| Topic download (wrapped) | `topic_pdfs/{title}_{topicId}.pdf` | `generate-pdf-lambda` |
| Chapter worksheet | `worksheets/{chapterTitle}-{tocId}.pdf` | `generate-pdf-lambda` |

Do **not** add chapter merge, topic download wrap, header/footer stamping, or post-wrap Adobe passes to this repo. Those belong in [`generate-pdf-lambda`](../../generate-pdf-lambda).

## Operational workflow

```
1. Remediate + sweep topic previews (this repo)
2. Validate previews in Acrobat (preview PDF only)
3. Upload previews to S3 when signed off
4. Admin → Generate worksheets (generate-pdf-lambda)
5. Validate wrapped topic + chapter PDFs
6. If step 5 fails but step 2 passed → fix generate-pdf-lambda, not this repo
```

## Anti-patterns (do not repeat)

- Re-sweeping topic previews because **chapter** worksheets still fail Acrobat
- Uploading previews and regenerating chapters in the same step before preview sign-off
- Adding `assemble_chapter.py`, `--post-wrap-a11y`, or worksheet overwrite logic here
- Storing chapter/topic-download PDFs under `tmp/` in this repo for migration work

## Local output layout

Preview work artifacts only:

```
tmp/{chapter-label}-previews-a11y/
  {topicId}-before.pdf    # downloaded from S3
  {topicId}-swept.pdf     # after post-remediation sweeps
  sweep-summary.json      # automated audit notes
```

Chapter/topic-download PDFs for wrap QA live outside this repo (e.g. `generate-pdf-lambda/tmp-*`).

## Scripts

| Script | Purpose |
|--------|---------|
| `scripts/migrate-channels-worksheets-a11y.sh` | Full Adobe + sweeps → write preview to S3 |
| `scripts/sweep-topic-previews.sh` | Sweeps only (no Adobe); local validation / re-sweep after code changes |

## Related docs

- [CHANNELS_WORKSHEET_A11Y_PLAN.md](./CHANNELS_WORKSHEET_A11Y_PLAN.md) — migration plan (preview-only v1; deferred chapter sections are historical)
