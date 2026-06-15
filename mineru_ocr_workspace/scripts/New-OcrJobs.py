import argparse
import csv
import hashlib
import json
import re
from collections import defaultdict
from datetime import datetime
from pathlib import Path


UPDATE_RE = re.compile(r"^-\s+\*\*文章更新日期\*\*\s*:\s*(.*?)\s*$", re.MULTILINE)
TITLE_RE = re.compile(r"^-\s+\*\*文章標題\*\*\s*:\s*(.*?)\s*$", re.MULTILINE)
URL_RE = re.compile(r"^-\s+\*\*文章網址\*\*\s*:\s*(.*?)\s*$", re.MULTILINE)


def stable_id(value: str) -> str:
    return hashlib.sha1(value.encode("utf-8")).hexdigest()[:16]


def sha1_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha1()
    with path.open("rb") as f:
        while True:
            chunk = f.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return path.read_text(encoding="cp950", errors="replace")


def first_match(pattern: re.Pattern, text: str) -> str:
    match = pattern.search(text)
    return match.group(1).strip() if match else ""


def priority_for(extension: str, source_bytes: int) -> int:
    ext = extension.lower()
    mb = 1024 * 1024
    if ext in {".jpg", ".jpeg", ".png", ".webp"} and source_bytes <= mb:
        return 10
    if ext == ".pdf" and source_bytes <= mb:
        return 20
    if ext in {".docx", ".xlsx", ".pptx"} and source_bytes <= mb:
        return 30
    if source_bytes <= 5 * mb:
        return 40
    return 90


def safe_int(value: str) -> int:
    try:
        return int(value)
    except Exception:
        return 0


def join_manifest_relative_path(root: Path, relative_path: str) -> Path:
    parts = [
        part
        for part in relative_path.replace("\\", "/").split("/")
        if part and part != "."
    ]
    return root.joinpath(*parts)


def main() -> int:
    parser = argparse.ArgumentParser(description="Create OCR jobs and status tracking from resolved related assets.")
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--source-md-dir", required=True)
    parser.add_argument("--www-root", required=True)
    parser.add_argument("--out-dir", required=True)
    parser.add_argument("--existing-ocr-dir", action="append", default=[])
    parser.add_argument(
        "--existing-inventory-csv",
        action="append",
        default=[],
        help="Completed OCR inventory CSV with assetId and primaryOcrMarkdownPath columns.",
    )
    parser.add_argument("--limit", type=int, default=0, help="Optional limit for job rows after sorting.")
    parser.add_argument("--hash-files", action="store_true", help="Compute SHA1 for each source asset.")
    args = parser.parse_args()

    manifest_path = Path(args.manifest)
    source_md_dir = Path(args.source_md_dir)
    www_root = Path(args.www_root)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    completed_cache = {}
    for inventory_text in args.existing_inventory_csv:
        inventory_path = Path(inventory_text)
        if not inventory_path.exists():
            continue
        with inventory_path.open("r", encoding="utf-8-sig", newline="") as f:
            for row in csv.DictReader(f):
                asset_id = row.get("assetId", "").strip()
                md_path = (
                    row.get("primaryOcrMarkdownPath", "").strip()
                    or row.get("ocrMarkdownPath", "").strip()
                    or row.get("cachedOcrMarkdownPath", "").strip()
                )
                if asset_id and md_path and Path(md_path).exists():
                    completed_cache.setdefault(asset_id, md_path)
    for cache_dir_text in args.existing_ocr_dir:
        cache_dir = Path(cache_dir_text)
        if not cache_dir.exists():
            continue
        for md_path in cache_dir.glob("*.md"):
            asset_id = md_path.name.split("_", 1)[0]
            completed_cache.setdefault(asset_id, str(md_path))

    md_meta_cache = {}

    def get_md_meta(md_relative_path: str) -> dict:
        if md_relative_path in md_meta_cache:
            return md_meta_cache[md_relative_path]
        md_path = source_md_dir / md_relative_path
        meta = {
            "articleTitle": "",
            "articleUrl": "",
            "articleUpdatedDate": "",
        }
        if md_path.exists():
            text = read_text(md_path)
            meta["articleTitle"] = first_match(TITLE_RE, text)
            meta["articleUrl"] = first_match(URL_RE, text)
            meta["articleUpdatedDate"] = first_match(UPDATE_RE, text)
        md_meta_cache[md_relative_path] = meta
        return meta

    assets = {}
    linked_md = defaultdict(list)
    linked_updates = defaultdict(list)
    linked_urls = defaultdict(list)

    with manifest_path.open("r", encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            if row.get("resolveStatus") != "resolved":
                continue
            if str(row.get("isOcrSupportedExtension", "")).lower() != "true":
                continue
            rel = row.get("resolvedRelativePath", "").strip()
            if not rel:
                continue
            if rel not in assets:
                assets[rel] = row
            md_rel = row.get("mdRelativePath", "")
            if md_rel:
                linked_md[rel].append(md_rel)
                meta = get_md_meta(md_rel)
                if meta["articleUpdatedDate"]:
                    linked_updates[rel].append(meta["articleUpdatedDate"])
                if meta["articleUrl"]:
                    linked_urls[rel].append(meta["articleUrl"])

    jobs = []
    for rel, row in assets.items():
        asset_id = stable_id(rel)
        source_path = join_manifest_relative_path(www_root, rel)
        source_exists = source_path.exists()
        source_bytes = source_path.stat().st_size if source_exists else safe_int(row.get("sourceBytes", ""))
        source_mtime = datetime.fromtimestamp(source_path.stat().st_mtime).isoformat(timespec="seconds") if source_exists else ""
        source_hash = sha1_file(source_path) if args.hash_files and source_exists else ""
        cache_path = completed_cache.get(asset_id, "")
        ocr_status = "completed_cached" if cache_path else "pending"
        max_update = max(linked_updates[rel]) if linked_updates[rel] else ""
        jobs.append({
            "assetId": asset_id,
            "priority": priority_for(row.get("extension", ""), source_bytes),
            "ocrStatus": ocr_status,
            "resolvedRelativePath": rel,
            "resolvedPath": str(source_path),
            "extension": row.get("extension", ""),
            "sourceBytes": source_bytes,
            "sourceMtime": source_mtime,
            "sourceSha1": source_hash,
            "cachedOcrMarkdownPath": cache_path,
            "ocrMarkdownPath": cache_path,
            "ocrGeneratedAt": "",
            "ocrChars": "",
            "ocrDurationSec": "",
            "attemptCount": 0,
            "lastError": "",
            "linkedMdCount": len(set(linked_md[rel])),
            "linkedMdPaths": " | ".join(sorted(set(linked_md[rel]))),
            "articleUpdatedDates": " | ".join(sorted(set(linked_updates[rel]))),
            "maxArticleUpdatedDate": max_update,
            "articleUrls": " | ".join(sorted(set(linked_urls[rel]))),
            "needsReocr": "N" if cache_path else "Y",
        })

    jobs.sort(key=lambda r: (int(r["priority"]), r["extension"], int(r["sourceBytes"]), r["resolvedRelativePath"].lower()))
    if args.limit > 0:
        jobs = jobs[:args.limit]

    fieldnames = [
        "assetId", "priority", "ocrStatus", "resolvedRelativePath", "resolvedPath",
        "extension", "sourceBytes", "sourceMtime", "sourceSha1",
        "cachedOcrMarkdownPath", "ocrMarkdownPath", "ocrGeneratedAt", "ocrChars",
        "ocrDurationSec", "attemptCount", "lastError", "linkedMdCount", "linkedMdPaths",
        "articleUpdatedDates", "maxArticleUpdatedDate", "articleUrls", "needsReocr",
    ]
    jobs_csv = out_dir / "ocr_jobs.csv"
    status_csv = out_dir / "ocr_asset_status.csv"
    for path in (jobs_csv, status_csv):
        with path.open("w", encoding="utf-8-sig", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(jobs)

    summary = {
        "generatedAt": datetime.now().isoformat(timespec="seconds"),
        "manifest": str(manifest_path),
        "sourceMdDir": str(source_md_dir),
        "wwwRoot": str(www_root),
        "outDir": str(out_dir),
        "jobRows": len(jobs),
        "completedCached": sum(1 for r in jobs if r["ocrStatus"] == "completed_cached"),
        "pending": sum(1 for r in jobs if r["ocrStatus"] == "pending"),
        "hashFiles": args.hash_files,
        "outputs": {
            "jobsCsv": str(jobs_csv),
            "statusCsv": str(status_csv),
        },
    }
    summary_path = out_dir / "ocr_jobs_summary.json"
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
