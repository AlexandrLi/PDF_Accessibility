# Option C spike: cpdf tag-aware worksheet wrap

**Date:** 2026-06-24  
**Pilot topic:** calculus / `9af3bc55` (Introduction to Limits)  
**Pilot chapter:** `917d0a39` — 3 topic previews merged

## Question

Can we rebuild topic download + chapter worksheets from Adobe-remediated **preview** PDFs while preserving accessibility tags, using **zero additional Adobe API calls**?

## Answer

**Yes for topic download and chapter merge** — [Coherent PDF (cpdf)](https://www.coherentpdf.com/) with `-process-struct-trees` preserves the logical structure tree through merge and decorative overlays. **pdf-lib (`generateTopicPdf`) destroys it completely.**

---

## Evidence (pilot PDFs from S3)

### Topic download — structure element counts

| File                                      | Pages | `/Marked` | Struct elements | `/Figure` tags |
| ----------------------------------------- | ----: | --------- | --------------: | -------------: |
| Preview (Adobe remediated) `9af3bc55.pdf` |     7 | ✅        |         **227** |             18 |
| pdf-lib download (`generateTopicPdf`)     |     8 | ❌        |           **0** |              0 |
| **cpdf wrap** (`step3-footer.pdf`)        |     8 | ✅        |         **227** |             18 |

cpdf pipeline used:

```bash
cpdf -merge cover.pdf preview.pdf -process-struct-trees -subformat PDF/UA-2 -o merged.pdf
cpdf -add-text "Introduction to Limits" ... -process-struct-trees -topleft "35 40" merged.pdf 2-end -o with-header.pdf
cpdf -add-text "Page %Page" ... -process-struct-trees -bottomright "70 15" with-header.pdf 2-end -o final.pdf
```

Headers/footers are stamped as **artifacts** (decorative), not content — cpdf keeps the main document structure intact.

### Chapter worksheet — structure element counts

| File                                    | Pages | `/Marked` | Struct elements | `/Figure` tags |
| --------------------------------------- | ----: | --------- | --------------: | -------------: |
| Preview sum (3 topics)                  |    22 | ✅        |         **450** |             58 |
| pdf-lib chapter (`generateChaptersPdf`) |    23 | ❌        |           **0** |              0 |
| **cpdf chapter merge**                  |    23 | ✅        |         **451** |             58 |

```bash
cpdf -merge cover.pdf preview1.pdf preview2.pdf preview3.pdf \
  -process-struct-trees -subformat PDF/UA-2 -o chapter.pdf
```

All figure alt-text structure from the three remediated previews survived the merge.

---

## Proposed production architecture

```
topic_pdfs/{topicId}.pdf          ← Adobe pipeline (ONLY Adobe pass)
        │
        ├─ cpdf merge(cover + preview [+ refs]) + artifact overlays
        │     → topic_pdfs/{title}_{topicId}.pdf
        │
        └─ cpdf merge(cover + preview₁ + preview₂ + …) + chapter artifact overlays
              → worksheets/{chapterTitle}-{tocId}.pdf
```

| Stage                     | Tool                         | Adobe?         |
| ------------------------- | ---------------------------- | -------------- |
| Preview remediation       | Existing Step Function       | **1× / topic** |
| Topic download wrap       | cpdf in migration fork       | No             |
| Chapter assembly          | cpdf in migration fork       | No             |
| Admin Generate worksheets | **Disable** for a11y courses | —              |

---

## cpdf operations mapped to `generateTopicPdf`

| `generateTopicPdf` (pdf-lib)   | cpdf replacement                                                             |
| ------------------------------ | ---------------------------------------------------------------------------- |
| Prepend `cover_study_prep.pdf` | `-merge cover.pdf preview.pdf -process-struct-trees`                         |
| Copy content pages             | Same merge (structure tree merged, not stripped)                             |
| `drawText` topic title         | `-add-text "…" -process-struct-trees` → **artifact**                         |
| `drawLine` header rule         | `-stamp-on` one-page line PDF **or** `-add-rectangle` (syntax TBD)           |
| `drawImage` logo               | `-stamp-on logo-stamp.pdf -process-struct-trees` → **artifact**              |
| `drawText` page footer         | `-add-text "Page %Page" -process-struct-trees -bottomright …` → **artifact** |
| References HTML → PDF          | **Risk area** — see below                                                    |

---

## Open items (before production)

### 1. References appendix (medium risk)

`generateTopicPdf` appends HTML references via Puppeteer/Chromium. That output is typically **untagged**. Merging it with `-process-struct-trees` adds pages but references may not be fully accessible.

**Mitigations:**

- Tag references HTML before PDF export (Playwright/Chromium tagged PDF mode)
- Merge references as artifact-only section (acceptable if references are supplementary)
- Omit references in v1 wrap if topic has none (common case)

### 2. Cover asset parity

Spike used `generate-pdf-lambda/src/assets/cover.pdf` (1 page). Production uses `cover_study_prep.pdf` (multi-page) — not in git; pull from deployed lambda assets or restore to fork `assets/worksheet/`.

Untagged cover pages merged with `-process-struct-trees` did not strip content tags from preview pages in the spike.

### 3. Logo + header line

Spike did not finish logo/line overlay (`-add-rectangle` coordinate syntax needs tuning). Recommended approach: pre-build a **one-page stamp PDF** (title bar + line + logo) and apply with `-stamp-on … -process-struct-trees` so stamp is artifact.

### 4. Licensing

cpdf is **AGPL-3.0**. Legal review needed for use in Pearson migration tooling / CI. Alternatives if AGPL blocked: commercial cpdf license from Coherent Graphics, or server-side isolation model.

### 5. Runtime packaging

| Environment                          | Approach                                                                                |
| ------------------------------------ | --------------------------------------------------------------------------------------- |
| Migration script (local / CodeBuild) | Bundle cpdf binary per OS/arch in fork                                                  |
| Lambda                               | Heavier — prefer run wrap in CodeBuild after Adobe passes, not in `generate-pdf-lambda` |

### 6. Validation gate

After wrap, run existing **Adobe Accessibility Checker** (audit-only, no autotag) on download + chapter outputs. Compare reports to preview baseline. Spike used pikepdf structure counts only — not a compliance sign-off.

---

## Recommendation

1. **Implement Stage B/C** in `PDF_Accessibility_fork` using cpdf (Python `subprocess` wrapper).
2. **Extend migration script** to write download + chapter keys after preview remediation (same run).
3. **Block** admin Generate worksheets / `generateTopicPdf` for courses with a11y migration metadata.
4. **Pilot validate** with Adobe checker + NVDA/VoiceOver on `step3-footer.pdf` before bulk rollout.

---

## Reproduce locally

```bash
cd PDF_Accessibility_fork
source .venv/bin/activate
pip install pikepdf

# Download cpdf binary (or brew install cpdf)
# See spike/cpdf-wrap/run_spike.sh

./spike/cpdf-wrap/run_spike.sh
python spike/cpdf-wrap/inspect_pdf.py spike/cpdf-wrap/output/step3-footer.pdf
```

Output artifacts: `spike/cpdf-wrap/output/`
