#!/usr/bin/env python
"""Build a consolidated inventory of completed OCR markdown assets."""

from __future__ import annotations

import argparse
import csv
import json
from collections import defaultdict
from datetime import datetime
from pathlib import Path


def read_csv_rows(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def main() -> int:
    parser = argparse.ArgumentParser(description="Create a global completed OCR inventory.")
    parser.add_argument("--workspace", default="mineru_ocr_workspace")
    parser.add_argument("--out-dir", default="mineru_ocr_workspace/ocr_completed_inventory")
    args = parser.parse_args()

    workspace = Path(args.workspace)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    metadata_by_asset_id: dict[str, dict[str, str]] = {}
    status_paths = sorted(workspace.rglob("ocr_asset_status.csv"))
    for status_path in status_paths:
        for row in read_csv_rows(status_path):
            asset_id = row.get("assetId", "").strip()
            if not asset_id:
                continue
            metadata_by_asset_id[asset_id] = {
                **row,
                "lastSeenStatusCsv": str(status_path),
            }

    md_by_asset_id: dict[str, list[Path]] = defaultdict(list)
    for md_dir in sorted(workspace.rglob("ocr_md_assets")):
        if not md_dir.is_dir():
            continue
        for md_path in sorted(md_dir.glob("*.md")):
            asset_id = md_path.name.split("_", 1)[0]
            if len(asset_id) == 16:
                md_by_asset_id[asset_id].append(md_path)

    rows: list[dict[str, str]] = []
    for asset_id, md_paths in sorted(md_by_asset_id.items()):
        md_paths = sorted(md_paths, key=lambda path: path.stat().st_mtime, reverse=True)
        primary = md_paths[0]
        meta = metadata_by_asset_id.get(asset_id, {})
        rows.append({
            "assetId": asset_id,
            "resolvedRelativePath": meta.get("resolvedRelativePath", ""),
            "resolvedPath": meta.get("resolvedPath", ""),
            "extension": meta.get("extension", ""),
            "sourceBytes": meta.get("sourceBytes", ""),
            "sourceMtime": meta.get("sourceMtime", ""),
            "linkedMdCount": meta.get("linkedMdCount", ""),
            "linkedMdPaths": meta.get("linkedMdPaths", ""),
            "articleUpdatedDates": meta.get("articleUpdatedDates", ""),
            "maxArticleUpdatedDate": meta.get("maxArticleUpdatedDate", ""),
            "articleUrls": meta.get("articleUrls", ""),
            "primaryOcrMarkdownPath": str(primary),
            "primaryOcrMarkdownBytes": str(primary.stat().st_size),
            "primaryOcrMarkdownMtime": datetime.fromtimestamp(primary.stat().st_mtime).isoformat(timespec="seconds"),
            "ocrMarkdownCopies": str(len(md_paths)),
            "allOcrMarkdownPaths": " | ".join(str(path) for path in md_paths),
            "lastSeenStatus": meta.get("ocrStatus", ""),
            "lastSeenStatusCsv": meta.get("lastSeenStatusCsv", ""),
        })

    fieldnames = [
        "assetId",
        "resolvedRelativePath",
        "resolvedPath",
        "extension",
        "sourceBytes",
        "sourceMtime",
        "linkedMdCount",
        "linkedMdPaths",
        "articleUpdatedDates",
        "maxArticleUpdatedDate",
        "articleUrls",
        "primaryOcrMarkdownPath",
        "primaryOcrMarkdownBytes",
        "primaryOcrMarkdownMtime",
        "ocrMarkdownCopies",
        "allOcrMarkdownPaths",
        "lastSeenStatus",
        "lastSeenStatusCsv",
    ]

    csv_path = out_dir / "completed_ocr_assets_inventory.csv"
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    summary = {
        "generatedAt": datetime.now().isoformat(timespec="seconds"),
        "workspace": str(workspace),
        "outDir": str(out_dir),
        "statusCsvScanned": len(status_paths),
        "uniqueCompletedAssets": len(rows),
        "ocrMarkdownFiles": sum(int(row["ocrMarkdownCopies"]) for row in rows),
        "outputs": {
            "inventoryCsv": str(csv_path),
        },
    }
    summary_path = out_dir / "completed_ocr_assets_inventory_summary.json"
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
