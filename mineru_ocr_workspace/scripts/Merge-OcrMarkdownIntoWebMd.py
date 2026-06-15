#!/usr/bin/env python
"""Merge bulk OCR Markdown outputs back into copied web Markdown files.

This is the bulk-OCR companion to Invoke-MinerURelatedOcr.ps1's legacy merge
step. It reads the related-assets manifest plus ocr_asset_status.csv and writes
augmented Markdown copies to a separate output directory without running OCR.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import shutil
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any


OCR_SECTION_RE = re.compile(
    r"(?ms)\r?\n---\r?\n\r?\n## 三、關聯檔案 OCR Markdown\r?\n.*\Z"
)
INLINE_OCR_BLOCK_RE = re.compile(
    r"(?ms)\r?\n?<!-- OCR_BEGIN relationIndex=.*?<!-- OCR_END -->\r?\n?"
)
RELATION_ITEM_RE = re.compile(r"^\s*-\s+\*\*\s*(\d+)\.\s*\[[^\]]+\]\*\*:\s*.*$")


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return path.read_text(encoding="cp950", errors="replace")


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def normalize_rel_path(value: str) -> str:
    value = (value or "").strip().replace("\\", "/")
    value = re.sub(r"^[./]+", "", value)
    value = re.sub(r"^/+", "", value)
    value = re.sub(r"/+", "/", value)
    return value


def truthy(value: str) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "y"}


def safe_int(value: str) -> int:
    try:
        return int(float((value or "").strip()))
    except Exception:
        return 0


def markdown_inline(value: str) -> str:
    return (value or "").replace("\n", " ").replace("\r", " ").strip()


def parse_linked_md_paths(value: str) -> list[str]:
    return [
        normalize_rel_path(part)
        for part in (value or "").split("|")
        if normalize_rel_path(part)
    ]


def resolve_existing_path(path_text: str, source_md_dir: Path, source_md_dir_name: str) -> Path | None:
    if not path_text:
        return None
    path_text = path_text.strip()
    candidate = Path(path_text)
    if candidate.exists():
        return candidate.resolve()

    normalized = normalize_rel_path(path_text)
    direct = source_md_dir / normalized
    if direct.exists():
        return direct.resolve()

    marker = f"/{source_md_dir_name}/"
    if marker in normalized:
        suffix = normalized.split(marker, 1)[1]
        candidate = source_md_dir / suffix
        if candidate.exists():
            return candidate.resolve()
    return None


def path_is_relative_to(child: Path, parent: Path) -> bool:
    try:
        child.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def build_status_index(status_rows: list[dict[str, str]]) -> tuple[dict[str, dict[str, str]], Counter[str]]:
    index: dict[str, dict[str, str]] = {}
    counts: Counter[str] = Counter()
    for row in status_rows:
        status = (row.get("ocrStatus") or "").strip()
        counts[status] += 1
        if not status.startswith("completed"):
            continue
        md_path_text = (
            row.get("ocrMarkdownPath", "").strip()
            or row.get("cachedOcrMarkdownPath", "").strip()
        )
        if not md_path_text:
            continue
        md_path = Path(md_path_text)
        if not md_path.exists():
            continue
        rel = normalize_rel_path(row.get("resolvedRelativePath", ""))
        if not rel:
            continue
        indexed = dict(row)
        indexed["ocrMarkdownPath"] = str(md_path.resolve())
        indexed["ocrChars"] = str(safe_int(indexed.get("ocrChars", "")) or len(read_text(md_path)))
        index[rel] = indexed
    return index, counts


def copy_source_tree(source_md_dir: Path, output_dir: Path) -> int:
    copied = 0
    for source in source_md_dir.rglob("*.md"):
        rel = source.relative_to(source_md_dir)
        target = output_dir / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        copied += 1
    return copied


def strip_generated_ocr(text: str) -> str:
    text = OCR_SECTION_RE.sub("", text)
    text = INLINE_OCR_BLOCK_RE.sub("\n", text)
    return text.rstrip()


def render_ocr_block(
    record: dict[str, str],
    args: argparse.Namespace,
    stats: dict[str, Any],
    heading_prefix: str,
    marker: bool,
) -> list[str]:
    ocr_path = record.get("ocrMarkdownPath", "")
    if not ocr_path:
        return []
    ocr_file = Path(ocr_path)
    if not ocr_file.exists():
        return []

    ocr_chars = safe_int(record.get("ocrChars", ""))
    if ocr_chars <= 0:
        ocr_chars = len(read_text(ocr_file))
    is_large = ocr_chars > args.large_markdown_chars
    relation_index = record.get("relationIndex", "")

    stats["ocrReferences"] += 1
    lines: list[str] = []
    if marker:
        lines.append(
            f"<!-- OCR_BEGIN relationIndex={relation_index} assetId={record.get('assetId', '')} -->"
        )
    lines.extend(
        [
            f"{heading_prefix} OCR {relation_index}: {markdown_inline(record.get('relationUrl', ''))}",
            "",
            f"- 關聯類型: {record.get('relationLabel', '')}",
            f"- 本機檔案: {record.get('resolvedRelativePath', '')}",
            f"- OCR Markdown: {ocr_path}",
            f"- OCR 狀態: {record.get('ocrStatus', '')}",
            f"- OCR 字數: {ocr_chars}",
            f"- 驗證狀態: {'oversized_for_llm_context' if is_large else 'ok'}",
            "",
        ]
    )
    if is_large:
        stats["oversizedAssetsMarked"] += 1
        if not args.append_oversized:
            stats["oversizedAssetsNotAppended"] += 1
            lines.extend(
                [
                    f"> 此 OCR Markdown 超過 LargeMarkdownChars={args.large_markdown_chars}，已標記但未內嵌全文；請改讀上方 OCR Markdown 檔案。",
                    "",
                ]
            )
            if marker:
                lines.append("<!-- OCR_END -->")
            return lines

    lines.append("```markdown")
    lines.append(read_text(ocr_file).rstrip())
    lines.append("```")
    lines.append("")
    stats["ocrAssetsAppended"] += 1
    if marker:
        lines.append("<!-- OCR_END -->")
    return lines


def write_append_merge(
    md_rel: str,
    records: list[dict[str, str]],
    source_md_dir: Path,
    output_dir: Path,
    args: argparse.Namespace,
    stats: dict[str, Any],
) -> bool:
    source = source_md_dir / md_rel
    target = output_dir / md_rel
    base_text = strip_generated_ocr(read_text(target if target.exists() else source))

    append_lines: list[str] = [
        "",
        "---",
        "",
        "## 三、關聯檔案 OCR Markdown",
        "",
        "> 以下內容由 MinerU 從關聯圖片或檔案產生。標記為 oversized_for_llm_context 的項目不建議直接整段放進 LLM 上下文。",
        "",
    ]

    seen_ocr_paths: set[str] = set()
    for record in sorted(records, key=lambda item: safe_int(item.get("relationIndex", ""))):
        ocr_path = record.get("ocrMarkdownPath", "")
        if not ocr_path:
            continue
        if ocr_path in seen_ocr_paths:
            stats["duplicateOcrReferencesSkipped"] += 1
            continue
        seen_ocr_paths.add(ocr_path)
        append_lines.extend(render_ocr_block(record, args, stats, "###", marker=False))

    if not seen_ocr_paths:
        return False
    write_text(target, base_text + "\n".join(append_lines))
    return True


def write_inline_merge(
    md_rel: str,
    records: list[dict[str, str]],
    source_md_dir: Path,
    output_dir: Path,
    args: argparse.Namespace,
    stats: dict[str, Any],
) -> bool:
    source = source_md_dir / md_rel
    target = output_dir / md_rel
    base_text = strip_generated_ocr(read_text(target if target.exists() else source))
    lines = base_text.splitlines()

    records_by_index: dict[int, list[dict[str, str]]] = defaultdict(list)
    for record in records:
        relation_index = safe_int(record.get("relationIndex", ""))
        if relation_index > 0:
            records_by_index[relation_index].append(record)

    inserted = 0
    output_lines: list[str] = []
    for line in lines:
        output_lines.append(line)
        match = RELATION_ITEM_RE.match(line)
        if not match:
            continue
        relation_index = safe_int(match.group(1))
        relation_records = records_by_index.get(relation_index, [])
        if not relation_records:
            continue

        seen_for_relation: set[str] = set()
        block_lines: list[str] = ["", "<!-- OCR inline block inserted by Merge-OcrMarkdownIntoWebMd.py -->"]
        for record in relation_records:
            ocr_path = record.get("ocrMarkdownPath", "")
            if not ocr_path:
                continue
            if ocr_path in seen_for_relation:
                stats["duplicateOcrReferencesSkipped"] += 1
                continue
            seen_for_relation.add(ocr_path)
            rendered = render_ocr_block(record, args, stats, "####", marker=True)
            if rendered:
                block_lines.extend(rendered)
                block_lines.append("")
        if seen_for_relation:
            output_lines.extend(block_lines)
            inserted += len(seen_for_relation)

    missing_indices = sorted(set(records_by_index) - {
        safe_int(match.group(1))
        for line in lines
        for match in [RELATION_ITEM_RE.match(line)]
        if match
    })
    stats["inlineRelationIndexesNotFound"] += len(missing_indices)
    stats["inlineOcrBlocksInserted"] += inserted
    if inserted == 0:
        return False
    write_text(target, "\n".join(output_lines).rstrip() + "\n")
    return True


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Append completed OCR Markdown outputs to copied web Markdown files."
    )
    parser.add_argument("--source-md-dir", required=True, type=Path)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--status-csv", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument(
        "--placement",
        choices=["append", "inline"],
        default="append",
        help="append writes one OCR section at the end of each Markdown file; inline inserts OCR below each related-file item.",
    )
    parser.add_argument("--large-markdown-chars", type=int, default=120000)
    parser.add_argument("--append-oversized", action="store_true")
    parser.add_argument(
        "--copy-all",
        action="store_true",
        help="Copy every source Markdown file before appending OCR sections. Otherwise only OCR-linked files are written.",
    )
    parser.add_argument("--summary-json", type=Path, default=None)
    args = parser.parse_args()

    source_md_dir = args.source_md_dir.resolve()
    output_dir = args.output_dir.resolve()
    manifest_path = args.manifest.resolve()
    status_csv = args.status_csv.resolve()

    if not source_md_dir.exists():
        raise FileNotFoundError(source_md_dir)
    if not manifest_path.exists():
        raise FileNotFoundError(manifest_path)
    if not status_csv.exists():
        raise FileNotFoundError(status_csv)
    if source_md_dir == output_dir or path_is_relative_to(output_dir, source_md_dir):
        raise SystemExit(f"Refusing to write output inside source Markdown dir: {output_dir}")

    output_dir.mkdir(parents=True, exist_ok=True)

    status_rows = read_csv(status_csv)
    status_by_rel, status_counts = build_status_index(status_rows)
    manifest_rows = read_csv(manifest_path)

    records_by_md: dict[str, list[dict[str, str]]] = defaultdict(list)
    skipped_manifest = Counter()
    source_md_dir_name = source_md_dir.name

    for row in manifest_rows:
        if row.get("resolveStatus") != "resolved":
            skipped_manifest["not_resolved"] += 1
            continue
        if not truthy(row.get("isOcrSupportedExtension", "")):
            skipped_manifest["unsupported_extension"] += 1
            continue
        rel = normalize_rel_path(row.get("resolvedRelativePath", ""))
        if not rel:
            skipped_manifest["missing_resolved_relative_path"] += 1
            continue
        status = status_by_rel.get(rel)
        if not status:
            skipped_manifest["no_completed_ocr"] += 1
            continue

        md_rel = normalize_rel_path(row.get("mdRelativePath", ""))
        if not md_rel:
            md_path = resolve_existing_path(row.get("mdPath", ""), source_md_dir, source_md_dir_name)
            if md_path:
                md_rel = normalize_rel_path(str(md_path.relative_to(source_md_dir)))
        if not md_rel:
            skipped_manifest["missing_md_relative_path"] += 1
            continue
        if not (source_md_dir / md_rel).exists():
            skipped_manifest["source_md_missing"] += 1
            continue

        merged = {
            **row,
            "ocrStatus": status.get("ocrStatus", ""),
            "ocrMarkdownPath": status.get("ocrMarkdownPath", ""),
            "ocrChars": status.get("ocrChars", ""),
            "assetId": status.get("assetId", ""),
        }
        records_by_md[md_rel].append(merged)

    copied_all_count = copy_source_tree(source_md_dir, output_dir) if args.copy_all else 0

    stats: dict[str, Any] = {
        "generatedAt": datetime.now().isoformat(timespec="seconds"),
        "sourceMdDir": str(source_md_dir),
        "manifest": str(manifest_path),
        "statusCsv": str(status_csv),
        "outputDir": str(output_dir),
        "placement": args.placement,
        "copyAll": bool(args.copy_all),
        "sourceMdCopied": copied_all_count,
        "statusCounts": dict(status_counts),
        "completedOcrAssetsWithExistingMarkdown": len(status_by_rel),
        "manifestRows": len(manifest_rows),
        "manifestSkipped": dict(skipped_manifest),
        "mdWithOcrRecords": len(records_by_md),
        "mdWrittenWithOcr": 0,
        "ocrReferences": 0,
        "ocrAssetsAppended": 0,
        "oversizedAssetsMarked": 0,
        "oversizedAssetsNotAppended": 0,
        "duplicateOcrReferencesSkipped": 0,
        "inlineOcrBlocksInserted": 0,
        "inlineRelationIndexesNotFound": 0,
    }

    for md_rel, records in sorted(records_by_md.items()):
        if args.placement == "inline":
            written = write_inline_merge(md_rel, records, source_md_dir, output_dir, args, stats)
        else:
            written = write_append_merge(md_rel, records, source_md_dir, output_dir, args, stats)
        if written:
            stats["mdWrittenWithOcr"] += 1

    if args.summary_json is None:
        args.summary_json = output_dir / "ocr_merge_summary.json"
    args.summary_json.parent.mkdir(parents=True, exist_ok=True)
    args.summary_json.write_text(json.dumps(stats, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(stats, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
