# PDF_Accessibility_fork

This repo repairs topic preview PDFs (`courses/{courseId}/topic_pdfs/{topicId}.pdf`)
and publishes them to the dev bucket `channels-data-dev`. Chapter PDFs are
merged from those previews by `generate-pdf-lambda` (`generate-chapters-pdf`),
so a chapter row only changes after its topics are fixed, pushed, and the
chapter is re-wrapped there.

## Python

Every accessibility, migration and sweep command runs in the shared root
`.venv` through the launcher:

```sh
scripts/with-a11y-python.sh <python arguments>
```

## Sweeping a course

```sh
scripts/accessibility-course-workflow.sh prepare <course-id> --all-topics
```

`--all-topics` sweeps every topic in the course default TOC and is the normal
mode. Without it `prepare` sweeps only the topics in the Aug 25 issue map.
Topics with `pdfAvailable=false` are listed under `skippedNoPdf` in the
report, and table checks run on every PDF that has a table.

`--resume` reuses diagnostics keyed by a fingerprint of `scripts/lib` alone,
so after editing `sweep_accessibility_issue_map.py` run without it.

A topic with no structure tree is untouched by every sweep stage. Run
`scripts/autotag-untagged-pdf.py` (Adobe Auto-Tag) on it first. Adobe repaints
glyphs, so the output renders differently and takes the render-exception path
below.

## Publishing

Publishing waits for the user's explicit go-ahead in the current session,
given after they open the generated PDFs in desktop Acrobat. The API checker
passes files that desktop Acrobat fails, so the API result alone is not
approval.

```sh
scripts/accessibility-course-workflow.sh plan <compact-manifest>
scripts/accessibility-course-workflow.sh publish <compact-manifest> --approved-sha <sha256>
```

`publish` is fail-closed: it preflights every object, verifies recovery for
every object, then replaces and reads back each PDF. The manifest is the
source of truth and each exception names one topic:

- Acrobat passes a topic the local checker marked residual or unverifiable:
  `--approved-exception <topic-id>`.
- A repaired PDF renders differently (Auto-Tag, OCR, `--allow-render-change`):
  the manifest keeps `renderIdentical: false`, the user reviews the pages,
  then `--approved-render-exception <topic-id>`.

A manifest field describes the file as it is, so `renderIdentical` is true
only for a file whose rendered pages are byte-identical to the original.
Exceptions are per topic; a course-wide exception is never the answer.

Roll back with the report the publish wrote:

```sh
scripts/accessibility-course-workflow.sh rollback <replacement-report>
```

## Progress tracker

[reports/ACCESSIBILITY_PROGRESS.md](reports/ACCESSIBILITY_PROGRESS.md) is the
record of what still fails the Adobe accessibility checker, per course id,
chapter and topic, and of what was found while fixing it. It lives in the
git-ignored `reports/` folder on this machine (user decision 2026-09-11) and
is the only record: progress and findings go into this file, not into chat or
an artifact.

- Update the tracker as part of the work it records, before reporting the
  work done. A sweep, a push to dev, a chapter re-wrap, or a re-validation of
  a sheet is done when the tracker says so.
- Tick a row (`- [ ]` to `- [x]`) when the workbook shows PASS for it after
  re-validation against the dev bucket, and append `(done YYYY-MM-DD, note)`
  with an absolute date and the sweep report or commit that fixed it. Pushed
  but not re-validated stays unticked.
- If a row is partly fixed, edit the rule list on the line instead of ticking.
- Record course-level events (sweep date, push date, re-wrap date, sheet
  re-validated on) in the sentence under the course heading.
- Rows are permanent. A row that fails again after being ticked goes back to
  `- [ ]` with a note saying when it regressed.
- Every line carries the chapter id or topic id from the course JSON on dev
  (`courses/{courseId}/{courseId}.json`, default TOC). New rows need them too.
- Write findings, not only ticks. A root cause, a spot check, a decision to
  defer, or a fix built but not yet deployed goes into Key findings, Next
  actions, or the course sentence, with an absolute date. Chapter rows that
  pass a check but await workbook re-validation get a dated note instead of a
  tick.
- Optional local view: `scripts/with-a11y-python.sh
  scripts/render_accessibility_progress.py` renders the file to
  `reports/accessibility-progress.html` for reading only.

## Facts that are not in the code

- Validation reads the dev bucket. PDFs are copied to prod afterwards, so dev
  is the only place to check.
- The Adobe checker fails Bookmarks only on documents over about 20 pages,
  so it shows on chapter PDFs and almost never on topic previews.
- The two Physics sheets in the workbook are `physics` ("Physics with
  calculu") and `physics-with-algebra` ("Physics with A"). Confirmed by the
  user on 2026-09-11.
- `biochemistry` was swept on 2026-08-26 but never pushed. Re-sweep and check
  before pushing.
- Full fix path for a course: sweep every topic here, publish to dev, re-run
  `generate-chapters-pdf` in `generate-pdf-lambda` for that course,
  re-validate the sheet, update the tracker.
