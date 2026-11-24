#!/usr/bin/env bash
set -euo pipefail

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
INPUT="$ROOT/sample_input"
OUT="${1:-$ROOT/out/reproduced-$(date -u +%Y%m%dT%H%M%SZ)}"
AS_OF="2026-01-06T00:00Z"

mkdir -p "$OUT"

echo "== Generating sample input =="
"$ROOT/sla-report" sample --dir "$INPUT"

echo "== Running tests =="
python3 -I "$ROOT/tests/test_sla.py"

echo "== Original report, cutoff 5 =="
"$ROOT/sla-report" report \
  --evidence "$INPUT/evidence.json" \
  --contracts "$INPUT/contracts.json" \
  --calendars "$INPUT/calendars.json" \
  --as-of "$AS_OF" \
  --cutoff 5 \
  --out "$OUT/report-cut5.json" \
  --csv "$OUT/report-cut5.csv"

echo "== New report, cutoff 10 =="
"$ROOT/sla-report" report \
  --evidence "$INPUT/evidence.json" \
  --contracts "$INPUT/contracts.json" \
  --calendars "$INPUT/calendars.json" \
  --as-of "$AS_OF" \
  --cutoff 10 \
  --out "$OUT/report-cut10.json" \
  --csv "$OUT/report-cut10.csv"

SIGNED="$OUT/DAILY-2026-01-05.signed.json"
echo "== Signing daily close =="
"$ROOT/sla-report" close \
  --evidence "$INPUT/evidence.json" \
  --contracts "$INPUT/contracts.json" \
  --calendars "$INPUT/calendars.json" \
  --as-of "$AS_OF" \
  --cutoff 5 \
  --close-id DAILY-2026-01-05 \
  --out "$SIGNED" \
  --csv "$OUT/DAILY-2026-01-05.csv"

"$ROOT/sla-report" verify --signed "$SIGNED"

echo "== Restating with cutoff 10 and visible calendar v2 =="
"$ROOT/sla-report" restate \
  --signed "$SIGNED" \
  --evidence "$INPUT/evidence.json" \
  --contracts "$INPUT/contracts.json" \
  --calendars "$INPUT/calendars.json" \
  --cutoff 10 \
  --out "$OUT/DAILY-2026-01-05.restated-cut10.json" \
  --csv "$OUT/DAILY-2026-01-05.restated-cut10.csv" \
  --changes-csv "$OUT/DAILY-2026-01-05.changes.csv"

echo "== Evidence-only restatement, calendar pinned to v1 =="
"$ROOT/sla-report" restate \
  --signed "$SIGNED" \
  --evidence "$INPUT/evidence.json" \
  --contracts "$INPUT/contracts.json" \
  --calendars "$INPUT/calendars.json" \
  --cutoff 10 \
  --calendar-version WEST=1 \
  --out "$OUT/evidence-only.json"

echo "== Calendar-only restatement, cutoff remains 5 =="
"$ROOT/sla-report" restate \
  --signed "$SIGNED" \
  --evidence "$INPUT/evidence.json" \
  --contracts "$INPUT/contracts.json" \
  --calendars "$INPUT/calendars.json" \
  --calendar-version WEST=2 \
  --out "$OUT/calendar-only.json"

echo
echo "Reproduced outputs in: $OUT"
