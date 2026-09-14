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
`--all-topics --topic-id <id>` (repeatable) narrows the sweep to those TOC
topics; ids missing from the TOC are listed under `unmatched`.
Topics with `pdfAvailable=false` are listed under `skippedNoPdf` in the
report, and table checks run on every PDF that has a table.

`--resume` reuses diagnostics keyed by a fingerprint of `scripts/lib` alone,
so after editing `sweep_accessibility_issue_map.py` run without it.

A topic with no structure tree is untouched by every sweep stage. Run
`scripts/autotag-untagged-pdf.py` (Adobe Auto-Tag) on it first. Adobe repaints
glyphs, so the output renders differently and takes the render-exception path
below.

## Unattended run

```sh
scripts/accessibility-course-workflow.sh auto <course-id> [--plan-only]
```

`auto` runs the whole flow for one course with no approval step:
sweep the topics with open rows in the tracker for that course (every
default-TOC topic with `--all-topics`), Adobe Auto-Tag any topic with no
structure tree and sweep the tagged file, run the Adobe PDF Services checker on every changed resolved
topic, publish the topics that pass, rebuild the course's downloads and
chapter books on dev and check them, append a dated sentence to the
tracker, and re-render `reports/accessibility-progress.html` from it. The policy lives in `scripts/lib/auto_course_pipeline.py`. A topic
is pushed only when the sweep says resolved with a stable second pass (a
residual whose only categories are Tables Headers or Tables Regularity also
qualifies, since the local table audit is stricter than Acrobat), the
file changed, the Adobe API reports zero failed rules, and the render is
identical or the topic was Auto-Tagged with no page changing more than
`--max-render-diff` (default 5%) of its pixels. Everything else lands in
`reports/<course>-auto-<timestamp>.auto.review.json` with the reasons.

The Adobe API passes files desktop Acrobat fails, so an `auto` push is not
Acrobat-verified. `auto` still ticks a pushed topic's tracker row itself when
the topic is high confidence: the render is byte-identical, Adobe marks
nothing "Needs manual check" beyond Logical Reading Order and Color contrast,
every rule named on the row passed, and no font lacks a ToUnicode map (the
known case where desktop Acrobat is stricter). The summary's `tick` stage
lists the rows ticked and why each other pushed row was not. Every other row
stays unticked until the user checks dev. Use `--plan-only` to run every stage
and write the replacement plan without touching S3. Roll back an `auto` push with `rollback` and the replacement
audit the summary names.

After a push that verified at least one object, `auto` invokes
`generate-pdf-{env}-generateCoursePdf` so the topic downloads and every TOC's
chapter books are rebuilt from the pushed previews, then waits for the default
TOC's books to land in S3 (a book counts as rebuilt when its ETag or
LastModified moves past the pre-invocation snapshot, so a rebuild to the same
bytes still counts). Lambda's async retries re-drive a book that runs past the
chapter lambda's 15-minute clock, so the wait runs up to `--rebuild-timeout`
(default 2700s, polling every `--rebuild-poll`) and survives a throttled poll.
It then invalidates the course's `topic_pdfs/*` and `worksheets/*` (the lambda
invalidates `worksheets/*` when it dispatches, minutes before the books exist),
runs the Adobe checker on each rebuilt book, and ticks the chapter rows whose
book passes every rule the row names, with nothing needing manual check beyond
Logical Reading Order and Color contrast and no font missing a ToUnicode map.
Two chapters that share a title in one TOC also share one worksheet key, so
neither row is ticked from the one book the lambda leaves there. By default only chapters with an open
row are checked; `--check-all-worksheets` checks every rebuilt book. The handler answers 500 when any one of its fan-out requests
failed, so `auto` reads the payload: a failed chapter-book request stops the
stage, while a failed topic download is recorded as a warning in the tracker
sentence and the wait goes ahead. The stage is dev-only, because every handler
in `generate-pdf-lambda` answers 403 outside the dev stage; `--skip-rebuild`
turns it off, and the `rebuild`, `worksheetCheck` and `chapterTick` stages in
the summary say what happened. An AWS fault during the stage is recorded as
`rebuild.error` rather than raised, because the push it follows has already
happened and the tracker sentence has to record it.
`--rebuild-only` runs that stage by itself against what is already on dev,
which is how to re-drive it after a timeout or for a course whose topics an
earlier run pushed. It rebuilds for real, so it refuses `--plan-only`.

## Publishing

Manual publishing waits for the user's explicit go-ahead in the current session,
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
- Ticking is the user's call, with one exception: `auto` ticks the topic rows
  it pushed as high confidence, and the chapter rows whose rebuilt book passes
  every rule the row names (see "Unattended run"). For everything else the
  user checks the course on dev after a push and says which rows (or which
  course) pass. Then tick those rows (`- [ ]` to `- [x]`), append
  `(done YYYY-MM-DD, note)` with an absolute date and the sweep report or
  commit that fixed it, and re-render the HTML so the marks carry over.
  Pushed but neither high confidence nor approved by the user stays unticked.
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
- Full fix path for a course: sweep every topic here, publish to dev, invoke
  the dev lambda `generate-pdf-dev-generateCoursePdf` with `{"courseId"}` so
  every topic download and every TOC's chapter books are rebuilt, invalidate
  `/courses/{courseId}/topic_pdfs/*` on CloudFront distribution
  `E27O7BO97BHXFO` (the lambda invalidates only `worksheets/*`, so the topic
  wraps stay cached; seen 2026-09-14), re-validate the sheet, update the
  tracker. `auto` does the invoke, the wait, both invalidations and the chapter
  check itself; run those by hand only after `--skip-rebuild` or a failed run.
- The UI never serves `topic_pdfs/{topicId}.pdf`. It serves the wraps
  `topic_pdfs/{title}_{topicId}.pdf` (and `_with_answer_key.pdf`) and
  `worksheets/{chapterTitle}-{tocId}.pdf`, which `generate-pdf-lambda` builds
  from the preview. A push to the preview changes nothing a user downloads
  until `generateCoursePdf` (or the topic and chapter lambdas) reruns. Found
  2026-09-14: algebra-trigonometry downloads were still the 2026-09-03 wraps
  after two days of preview pushes. The `_with_answer_key` wraps fail Tagged
  content on their Chromium-rendered key pages; that is a lambda defect
  (answer-key branch), not a preview problem, and the workbook does not track
  those files.
