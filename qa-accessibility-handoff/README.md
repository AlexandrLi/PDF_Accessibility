# QA accessibility handoff — calculus preview PDFs

**Course:** calculus  
**Chapter:** 1. Limits and Continuity (`917d0a39`)  
**Environment:** dev (`channels-data-dev`)  
**Migration run:** `20260629T121947Z` (2026-06-29)

Send the `previews/` folder or the zip to QA.

---

## Files to test (3 topic preview PDFs)

These are **topic preview** files — what appears in the Guided Worksheet iframe (`topic_pdfs/{topicId}.pdf`).

| # | File | Topic ID | Title |
|---|------|----------|-------|
| 1 | `previews/01-Introduction-to-Limits_preview_9af3bc55.pdf` | `9af3bc55` | Introduction to Limits |
| 2 | `previews/02-Finding-Limits-Algebraically_preview_330b3fa8.pdf` | `330b3fa8` | Finding Limits Algebraically |
| 3 | `previews/03-Continuity_preview_f6df021b.pdf` | `f6df021b` | Continuity |

---

## Pipeline applied to each preview

1. Adobe accessibility remediation (Step Function)
2. Post-remediation figure alt repair (merge-related gaps)
3. Post-remediation layout table repair (unwrap mis-tagged `/Table` regions)

Topic download and chapter worksheet PDFs are **out of scope** for this handoff.

---

## Expected Adobe checker results

| Check | Expected |
|-------|----------|
| Figures alternate text | **Pass** |
| Tables → Headers / Summary | **Pass** (layout tables unwrapped or annotated) |
| Character encoding | **May still fail** on manual math PDFs (CID fonts) |

---

## Suggested QA checklist

- [ ] Screen reader reading order (NVDA / JAWS / VoiceOver) on all 3 topics
- [ ] Figure alt text on math diagrams
- [ ] Headings and lists announced correctly
- [ ] Adobe Acrobat Accessibility Checker on each PDF
- [ ] Spot-check in dev app: calculus → **1. Limits and Continuity** → each topic preview iframe

---

## Zip archive

`../qa-accessibility-handoff-calculus-previews-20260629.zip`
