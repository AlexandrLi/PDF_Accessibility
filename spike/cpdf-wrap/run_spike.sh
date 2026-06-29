#!/usr/bin/env bash
# Reproduce cpdf tag-aware wrap spike on calculus pilot topic 9af3bc55.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
CPDF="${CPDF:-$ROOT/cpdf-binaries-master/MacOS-ARM/cpdf}"
IN="$ROOT/input"
OUT="$ROOT/output"
TITLE="Introduction to Limits"

mkdir -p "$IN" "$OUT"

if [[ ! -x "$CPDF" ]]; then
  echo "cpdf not found at $CPDF — download from https://github.com/coherentgraphics/cpdf-binaries"
  exit 1
fi

if [[ ! -f "$IN/preview-9af3bc55.pdf" ]]; then
  echo "Downloading pilot preview from S3..."
  aws s3 cp "s3://channels-data-dev/courses/calculus/topic_pdfs/9af3bc55.pdf" \
    "$IN/preview-9af3bc55.pdf" --region us-east-1
fi

if [[ ! -f "$IN/cover.pdf" ]]; then
  COVER_SRC="$(cd "$ROOT/../../.." && pwd)/generate-pdf-lambda/src/assets/cover.pdf"
  if [[ -f "$COVER_SRC" ]]; then
    cp "$COVER_SRC" "$IN/cover.pdf"
  else
    echo "Place cover.pdf in $IN"
    exit 1
  fi
fi

echo "=== Topic wrap spike ==="
"$CPDF" -merge "$IN/cover.pdf" "$IN/preview-9af3bc55.pdf" \
  -process-struct-trees -subformat PDF/UA-2 -o "$OUT/step1-merged.pdf"

"$CPDF" -add-text "$TITLE" -font Helvetica -font-size 12 -process-struct-trees \
  -topleft "35 40" "$OUT/step1-merged.pdf" 2-end -o "$OUT/step2-header.pdf"

"$CPDF" -add-text "Page %Page" -font Helvetica -font-size 10 -process-struct-trees \
  -bottomright "70 15" "$OUT/step2-header.pdf" 2-end -o "$OUT/step3-footer.pdf"

echo "Wrote $OUT/step3-footer.pdf"
echo "Inspect with: python $ROOT/inspect_pdf.py $OUT/step3-footer.pdf"
