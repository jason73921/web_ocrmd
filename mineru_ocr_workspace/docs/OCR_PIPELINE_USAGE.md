# OCR Pipeline Script Usage

This document records the production flow used for the platform run. Paths below are the remote Linux platform paths unless noted.

## Inputs

| Item | Path |
|---|---|
| Source web Markdown | `/mnt/project/input/md/網站文章MD_更新/網站文章MD` |
| Local website assets | `/mnt/project/input/www` |
| Related-assets manifest | `/mnt/project/cache/manifest_package/related_assets_manifest.csv` |
| OCR status CSV | `/mnt/project/output/ocr_run/ocr_asset_status.csv` |
| OCR output dir | `/mnt/project/output/ocr_run` |

## 1. Build OCR Jobs

The bulk route starts from `related_assets_manifest.csv` and deduplicates related assets by `resolvedRelativePath`.

```bash
cd /mnt/project/src/web_ocr2md/mineru_ocr_workspace

python scripts/New-OcrJobs.py \
  --manifest /mnt/project/cache/manifest_package/related_assets_manifest.csv \
  --source-md-dir /mnt/project/input/md/網站文章MD_更新/網站文章MD \
  --www-root /mnt/project/input/www \
  --out-dir /mnt/project/output/ocr_run \
  --existing-ocr-dir /mnt/project/cache/ocr_completed_cache/ocr_md_assets
```

Outputs:

| File | Purpose |
|---|---|
| `ocr_jobs.csv` | Deduplicated OCR job list |
| `ocr_asset_status.csv` | Resumable status table |
| `ocr_jobs_summary.json` | Job generation summary |

## 2. Run OCR on the Platform

For H200 platform processing, the stable production run used parallel independent API workers with tables disabled.

```bash
cd /mnt/project/src/web_ocr2md/mineru_ocr_workspace

stamp=$(date +%Y%m%d_%H%M%S)
shard=/mnt/project/output/ocr_run/parallel_shards_b16_${stamp}
log=/mnt/project/output/ocr_run/parallel_shards_b16_${stamp}.launcher.log

nohup .venv/bin/python scripts/Run-OcrPendingShards.py \
  --jobs-csv /mnt/project/output/ocr_run/ocr_asset_status.csv \
  --out-dir /mnt/project/output/ocr_run \
  --shard-root "$shard" \
  --workers 4 \
  --rows-per-worker 100 \
  --max-rounds 20 \
  --batch-size 16 \
  --processing-window-size 16 \
  --timeout-sec 1800 \
  --chunk-timeout-sec 7200 \
  --disable-table > "$log" 2>&1 &
```

Why this route:

- It keeps several MinerU FastAPI workers alive instead of restarting MinerU per file.
- Each worker owns a shard CSV/output directory, avoiding concurrent writes to the same CSV.
- `--disable-table` avoids the table-structure extraction path that was the main crash/slowdown source in this dataset.
- Markdown text OCR still runs; only structured table recognition is disabled.

Monitor:

```bash
tail -f /mnt/project/output/ocr_run/parallel_shards_b16_<stamp>/parallel_driver.log

nvidia-smi --query-gpu=utilization.gpu,utilization.memory,memory.used,memory.total,power.draw \
  --format=csv,noheader,nounits
```

Validate final OCR status:

```bash
python - <<'PY'
import csv
from collections import Counter
from pathlib import Path

p = Path('/mnt/project/output/ocr_run/ocr_asset_status.csv')
rows = list(csv.DictReader(p.open(encoding='utf-8-sig', newline='')))
print(dict(Counter(r.get('ocrStatus', '') for r in rows)))
missing = [
    r for r in rows
    if r.get('ocrStatus', '').startswith('completed')
    and (not r.get('ocrMarkdownPath') or not Path(r['ocrMarkdownPath']).exists())
]
print('completed missing markdown', len(missing))
PY
```

## 3. Merge OCR Into Web Markdown

The final accepted output is inline placement: OCR blocks are inserted directly below each related-file item in `## 二、關聯的檔案資訊`.

```bash
cd /mnt/project/src/web_ocr2md/mineru_ocr_workspace

python scripts/Merge-OcrMarkdownIntoWebMd.py \
  --source-md-dir /mnt/project/input/md/網站文章MD_更新/網站文章MD \
  --manifest /mnt/project/cache/manifest_package/related_assets_manifest.csv \
  --status-csv /mnt/project/output/ocr_run/ocr_asset_status.csv \
  --output-dir /mnt/project/output/web_md_with_ocr_inline_20260615 \
  --placement inline \
  --copy-all \
  --summary-json /mnt/project/output/web_md_with_ocr_inline_20260615/ocr_merge_summary.json
```

Inline output shape:

````markdown
- **1. [超連結（檔案）]**: files/example.pdf

<!-- OCR_BEGIN relationIndex=1 assetId=... -->
#### OCR 1: files/example.pdf

- 關聯類型: 超連結（檔案）
- 本機檔案: admin/.../example.pdf
- OCR Markdown: /mnt/project/output/ocr_run/ocr_md_assets/...
- OCR 狀態: completed
- OCR 字數: 1234
- 驗證狀態: ok

```markdown
OCR text...
```
<!-- OCR_END -->
````

The older append-at-end mode is still available for comparison:

```bash
python scripts/Merge-OcrMarkdownIntoWebMd.py \
  --source-md-dir /mnt/project/input/md/網站文章MD_更新/網站文章MD \
  --manifest /mnt/project/cache/manifest_package/related_assets_manifest.csv \
  --status-csv /mnt/project/output/ocr_run/ocr_asset_status.csv \
  --output-dir /mnt/project/output/web_md_with_ocr_20260615 \
  --placement append \
  --copy-all
```

## 4. Build the Final Report

```bash
cd /mnt/project/src/web_ocr2md/mineru_ocr_workspace
python scripts/Build-FinalReport.py
```

Outputs:

| File | Purpose |
|---|---|
| `/mnt/project/output/final_report_20260615/FINAL_REPORT.md` | Human-readable final report |
| `/mnt/project/output/final_report_20260615/FINAL_REPORT_SUMMARY.json` | Machine-readable summary |
| `/mnt/project/output/final_report_20260615/*.csv` | Full issue/result lists |

## Final Production Outputs

| Item | Path |
|---|---|
| Final merged Markdown | `/mnt/project/output/web_md_with_ocr_inline_20260615` |
| Final report folder | `/mnt/project/output/final_report_20260615` |
| OCR Markdown from this run | `/mnt/project/output/ocr_run/ocr_md_assets` |
| Cached OCR Markdown used | `/mnt/project/cache/ocr_completed_cache/ocr_md_assets` |
| OCR status CSV | `/mnt/project/output/ocr_run/ocr_asset_status.csv` |
