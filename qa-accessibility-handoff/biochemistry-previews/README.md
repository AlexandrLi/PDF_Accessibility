# QA accessibility handoff — biochemistry preview PDFs

**Course:** biochemistry  
**Chapter:** 1. Introduction to Biochemistry (`15326d05`, toc `bcddb411`)  
**Environment:** dev (`channels-data-dev`)  
**Migration run:** `20260629T130522Z` (2026-06-29)

Send the PDFs in this folder (or the zip at repo root) to QA.

---

## Files to test (3 topic preview PDFs)

These are **topic preview** files — what appears in the Guided Worksheet iframe (`topic_pdfs/{topicId}.pdf`).

| # | File | Topic ID | Title |
|---|------|----------|-------|
| 1 | `01-What-is-Biochemistry_preview_3c0ea4da.pdf` | `3c0ea4da` | What is Biochemistry? |
| 2 | `02-Characteristics-of-Life_preview_cd099d0a.pdf` | `cd099d0a` | Characteristics of Life |
| 3 | `03-Abiogenesis_preview_8cf09d21.pdf` | `8cf09d21` | Abiogenesis |

---

## Pipeline applied

1. Adobe accessibility remediation (Step Function)
2. Post-remediation figure alt repair
3. Post-remediation layout table repair

---

## Migration notes

| Topic | Alt repair | Table repair |
|-------|------------|--------------|
| `3c0ea4da` | figures 3, 6, 7 | none |
| `cd099d0a` | none | 2 tables annotated (Summary + headers) |
| `8cf09d21` | none | none |

---

## Expected Adobe checker results

| Check | Expected |
|-------|----------|
| Figures alternate text | **Pass** |
| Tables → Headers / Summary | **Pass** (or N/A if no tables) |
| Character encoding | **May still fail** on manual/scanned PDFs |

---

## Zip archive

`../../qa-accessibility-handoff-biochemistry-previews-20260629.zip`

## App URLs (dev)

Spot-check in the web app: **biochemistry** → **1. Introduction to Biochemistry** → each topic preview iframe.
