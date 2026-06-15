#!/usr/bin/env python
"""Build the final Web Markdown / related-asset OCR report."""

from __future__ import annotations

import csv
import json
import re
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any


SECTION_RE = re.compile(r"(?ms)^##\s*二、關聯的檔案資訊\s*(.*?)(?:^---\s*$|^##\s+|\Z)")
REL_RE = re.compile(r"^\s*-\s*\*\*\s*(\d+)\.\s*\[([^\]]+)\]\*\*:\s*(.*?)\s*$", re.M)
TITLE_RE = re.compile(r"^-\s+\*\*文章標題\*\*\s*:\s*(.*?)\s*$", re.M)
URL_RE = re.compile(r"^-\s+\*\*文章網址\*\*\s*:\s*(.*?)\s*$", re.M)
DATE_RE = re.compile(r"^-\s+\*\*文章更新日期\*\*\s*:\s*(.*?)\s*$", re.M)


def clean_text(value: Any) -> Any:
    if isinstance(value, str):
        return value.encode("utf-8", errors="replace").decode("utf-8", errors="replace")
    if isinstance(value, dict):
        return {clean_text(k): clean_text(v) for k, v in value.items()}
    if isinstance(value, list):
        return [clean_text(v) for v in value]
    return value


def read_text(path: Path) -> str:
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        text = path.read_text(encoding="cp950", errors="replace")
    return clean_text(text)


def first(pattern: re.Pattern[str], text: str) -> str:
    match = pattern.search(text)
    return match.group(1).strip() if match else ""


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="", errors="replace") as handle:
        return [clean_text(dict(row)) for row in csv.DictReader(handle)]


def unique_fieldnames(fieldnames: list[str]) -> list[str]:
    output: list[str] = []
    seen: set[str] = set()
    for name in fieldnames:
        if name in seen:
            continue
        output.append(name)
        seen.add(name)
    return output


def write_csv(path: Path, rows: list[dict[str, Any]], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = unique_fieldnames(fieldnames)
    cleaned_rows = [clean_text(row) for row in rows]
    with path.open("w", encoding="utf-8-sig", newline="", errors="replace") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(cleaned_rows)


def truthy(value: str) -> bool:
    return str(value or "").strip().lower() in {"true", "1", "yes", "y"}


def norm(value: str) -> str:
    return str(value or "").replace("\\", "/").strip().lstrip("./")


def file_count(path: Path, pattern: str = "*") -> int:
    return sum(1 for item in path.rglob(pattern) if item.is_file()) if path.exists() else 0


def table_counts(counter: Counter[str], limit: int = 20) -> str:
    return "\n".join(f"| {key or '(blank)'} | {value} |" for key, value in counter.most_common(limit))


def categorize_ocr_error(message: str) -> str:
    low = (message or "").lower()
    if "unsupported file type" in low:
        return "unsupported_file_type"
    if "incorrect password" in low or "password" in low or "encrypted" in low:
        return "password_or_encrypted_pdf"
    if "cannot identify image file" in low:
        return "cannot_identify_image"
    if "empty" in low:
        return "empty_file"
    if "data format error" in low or "pdfium could not open" in low:
        return "corrupt_or_invalid_pdf"
    if "non-jpeg content" in low or "non-png content" in low:
        return "extension_content_mismatch"
    if "timeout" in low:
        return "timeout"
    if "connection" in low:
        return "api_connection_or_crash"
    return "other"


def main() -> int:
    source_md_dir = Path("/mnt/project/input/md/網站文章MD_更新/網站文章MD")
    manifest_path = Path("/mnt/project/cache/manifest_package/related_assets_manifest.csv")
    status_path = Path("/mnt/project/output/ocr_run/ocr_asset_status.csv")
    inline_dir = Path("/mnt/project/output/web_md_with_ocr_inline_20260615")
    append_dir = Path("/mnt/project/output/web_md_with_ocr_20260615")
    report_dir = Path("/mnt/project/output/final_report_20260615")
    report_dir.mkdir(parents=True, exist_ok=True)

    manifest = read_csv(manifest_path)
    status_rows = read_csv(status_path)

    md_rows: list[dict[str, Any]] = []
    md_by_rel: dict[str, dict[str, Any]] = {}
    for path in sorted(source_md_dir.rglob("*.md")):
        rel = norm(str(path.relative_to(source_md_dir)))
        text = read_text(path)
        section = SECTION_RE.search(text)
        links = REL_RE.findall(section.group(1) if section else "")
        row = {
            "mdRelativePath": rel,
            "mdPath": str(path),
            "articleTitle": first(TITLE_RE, text),
            "articleUrl": first(URL_RE, text),
            "articleUpdatedDate": first(DATE_RE, text),
            "hasRelatedSection": "Y" if section else "N",
            "parsedRelationLinkCount": len(links),
        }
        md_rows.append(row)
        md_by_rel[rel] = row

    manifest_md_set = {norm(row.get("mdRelativePath", "")) for row in manifest if norm(row.get("mdRelativePath", ""))}
    md_without_manifest_links = [row for row in md_rows if row["mdRelativePath"] not in manifest_md_set]
    md_with_manifest_links = [row for row in md_rows if row["mdRelativePath"] in manifest_md_set]

    resolve_counts = Counter(row.get("resolveStatus", "") for row in manifest)
    resolved = [row for row in manifest if row.get("resolveStatus") == "resolved"]
    unresolved = [row for row in manifest if row.get("resolveStatus") != "resolved"]
    resolved_ocr_supported = [row for row in resolved if truthy(row.get("isOcrSupportedExtension", ""))]
    resolved_not_ocr_supported = [row for row in resolved if not truthy(row.get("isOcrSupportedExtension", ""))]
    unresolved_ocr_supported = [row for row in unresolved if truthy(row.get("isOcrSupportedExtension", ""))]
    unresolved_not_ocr_supported = [row for row in unresolved if not truthy(row.get("isOcrSupportedExtension", ""))]
    all_not_ocr_supported = [row for row in manifest if not truthy(row.get("isOcrSupportedExtension", ""))]
    unique_ocr_candidate_assets = {
        norm(row.get("resolvedRelativePath", ""))
        for row in resolved_ocr_supported
        if norm(row.get("resolvedRelativePath", ""))
    }

    status_counts = Counter(row.get("ocrStatus", "") for row in status_rows)
    ocr_errors = [row for row in status_rows if row.get("ocrStatus") == "error"]
    completed = [row for row in status_rows if row.get("ocrStatus") == "completed"]
    completed_cached = [row for row in status_rows if row.get("ocrStatus") == "completed_cached"]
    completed_any = [row for row in status_rows if str(row.get("ocrStatus", "")).startswith("completed")]
    completed_missing_md = []
    for row in completed_any:
        markdown_path = Path(row.get("ocrMarkdownPath", "")) if row.get("ocrMarkdownPath") else None
        if not markdown_path or not markdown_path.exists():
            completed_missing_md.append(row)

    for row in ocr_errors:
        row["errorCategory"] = categorize_ocr_error(row.get("lastError", ""))
    ocr_error_counts = Counter(row["errorCategory"] for row in ocr_errors)

    inline_summary_path = inline_dir / "ocr_merge_summary.json"
    inline_summary = json.loads(read_text(inline_summary_path)) if inline_summary_path.exists() else {}

    def enrich_manifest_rows(rows: list[dict[str, str]]) -> list[dict[str, Any]]:
        output = []
        for row in rows:
            md_rel = norm(row.get("mdRelativePath", ""))
            meta = md_by_rel.get(md_rel, {})
            output.append(
                {
                    "mdRelativePath": md_rel,
                    "articleTitle": meta.get("articleTitle", ""),
                    "articleUrl": row.get("articleUrl") or meta.get("articleUrl", ""),
                    "relationIndex": row.get("relationIndex", ""),
                    "relationLabel": row.get("relationLabel", ""),
                    "relationUrl": row.get("relationUrl", ""),
                    "decodedUrl": row.get("decodedUrl", ""),
                    "extension": row.get("extension", ""),
                    "isOcrSupportedExtension": row.get("isOcrSupportedExtension", ""),
                    "resolveStatus": row.get("resolveStatus", ""),
                    "resolveMethod": row.get("resolveMethod", ""),
                    "resolutionStage": row.get("resolutionStage", ""),
                    "resolutionConfidence": row.get("resolutionConfidence", ""),
                    "candidateCount": row.get("candidateCount", ""),
                    "candidatePaths": row.get("candidatePaths", ""),
                    "resolvedRelativePath": row.get("resolvedRelativePath", ""),
                    "sourceBytes": row.get("sourceBytes", ""),
                    "error": row.get("error", ""),
                }
            )
        return output

    manifest_fieldnames = [
        "mdRelativePath", "articleTitle", "articleUrl", "relationIndex", "relationLabel",
        "relationUrl", "decodedUrl", "extension", "isOcrSupportedExtension", "resolveStatus",
        "resolveMethod", "resolutionStage", "resolutionConfidence", "candidateCount",
        "candidatePaths", "resolvedRelativePath", "sourceBytes", "error",
    ]
    write_csv(report_dir / "md_without_related_links.csv", md_without_manifest_links, [
        "mdRelativePath", "mdPath", "articleTitle", "articleUrl", "articleUpdatedDate",
        "hasRelatedSection", "parsedRelationLinkCount",
    ])
    write_csv(report_dir / "unresolved_links.csv", enrich_manifest_rows(unresolved), manifest_fieldnames)
    write_csv(report_dir / "unresolved_ocr_supported_links.csv", enrich_manifest_rows(unresolved_ocr_supported), manifest_fieldnames)
    write_csv(report_dir / "unresolved_not_ocr_supported_links.csv", enrich_manifest_rows(unresolved_not_ocr_supported), manifest_fieldnames)
    write_csv(report_dir / "not_ocr_supported_links.csv", enrich_manifest_rows(all_not_ocr_supported), manifest_fieldnames)
    write_csv(report_dir / "resolved_not_ocr_supported_links.csv", enrich_manifest_rows(resolved_not_ocr_supported), manifest_fieldnames)
    status_fieldnames = unique_fieldnames(list(status_rows[0].keys()) + ["errorCategory"]) if status_rows else ["errorCategory"]
    write_csv(report_dir / "ocr_error_assets.csv", ocr_errors, status_fieldnames)
    write_csv(report_dir / "completed_missing_markdown.csv", completed_missing_md, list(status_rows[0].keys()) if status_rows else [])

    def sample_lines(rows: list[dict[str, Any]], limit: int = 12) -> str:
        output = []
        for row in rows[:limit]:
            url = str(row.get("relationUrl", ""))[:120].replace("|", "/")
            output.append(
                f"| `{row.get('mdRelativePath', '')}` | {row.get('relationIndex', '')} | "
                f"{row.get('relationLabel', '')} | `{row.get('extension', '') or '(none)'}` | "
                f"{row.get('resolveStatus', '')} | {url} |"
            )
        return "\n".join(output)

    report = f"""# Web Markdown OCR 最終報告

產生時間：{datetime.now().isoformat(timespec='seconds')}

## 一、資料位置

| 類型 | 路徑 |
|---|---|
| 原始 Web Markdown | `{source_md_dir}` |
| 關聯檔案掃描 manifest | `{manifest_path}` |
| OCR 主狀態 CSV | `{status_path}` |
| 本次平台新增 OCR Markdown | `/mnt/project/output/ocr_run/ocr_md_assets` |
| 既有 OCR 快取 Markdown | `/mnt/project/cache/ocr_completed_cache/ocr_md_assets` |
| MinerU raw output | `/mnt/project/output/ocr_run/mineru_raw` |
| 最終 inline 合併 Markdown | `{inline_dir}` |
| 文末集中版合併 Markdown（保留不用） | `{append_dir}` |
| 本報告資料夾 | `{report_dir}` |

## 二、Web Markdown 掃描總結

| 指標 | 數量 |
|---|---:|
| 原始 Web Markdown 檔案 | {len(md_rows):,} |
| 有至少一筆關聯連結的 Markdown | {len(md_with_manifest_links):,} |
| 沒有掃到關聯連結的 Markdown | {len(md_without_manifest_links):,} |
| 關聯連結總筆數（manifest rows） | {len(manifest):,} |
| 可解析到本機檔案/資產的連結 | {len(resolved):,} |
| 解析不到、格式不對、或非本機檔案的連結 | {len(unresolved):,} |
| 可解析且副檔名可 OCR 的關聯記錄 | {len(resolved_ocr_supported):,} |
| 可解析但副檔名/類型不進 OCR 的關聯記錄 | {len(resolved_not_ocr_supported):,} |
| 去重後可 OCR 的唯一資產 | {len(unique_ocr_candidate_assets):,} |

## 三、連結解析狀態

| resolveStatus | 筆數 |
|---|---:|
{table_counts(resolve_counts)}

### 解析不到的連結

完整清單：`{report_dir / 'unresolved_links.csv'}`

| 分類 | 筆數 |
|---|---:|
| 解析不到但副檔名原本可 OCR | {len(unresolved_ocr_supported):,} |
| 解析不到且本來就不是 OCR 支援類型 | {len(unresolved_not_ocr_supported):,} |

解析不到但原本可 OCR 的清單：`{report_dir / 'unresolved_ocr_supported_links.csv'}`

解析不到且不可 OCR 的清單：`{report_dir / 'unresolved_not_ocr_supported_links.csv'}`

### 解析不到樣本

| md | index | 類型 | ext | status | URL |
|---|---:|---|---|---|---|
{sample_lines(enrich_manifest_rows(unresolved), 12)}

## 四、沒有關聯連結的 Markdown

沒有掃到任何關聯連結的 Markdown：{len(md_without_manifest_links):,} 個。

完整清單：`{report_dir / 'md_without_related_links.csv'}`

這裡的「沒有關聯連結」是指掃描 manifest 沒有該 md 的關聯項目；不是指文章正文沒有任何網址文字。

## 五、不可 OCR 的連結/資產

| 類型 | 筆數 |
|---|---:|
| 所有不屬於 OCR 支援副檔名/類型的關聯記錄 | {len(all_not_ocr_supported):,} |
| 其中已解析到本機但不進 OCR | {len(resolved_not_ocr_supported):,} |
| 其中解析不到且也不進 OCR | {len(unresolved_not_ocr_supported):,} |

完整不可 OCR 清單：`{report_dir / 'not_ocr_supported_links.csv'}`

已解析但不 OCR 清單：`{report_dir / 'resolved_not_ocr_supported_links.csv'}`

## 六、OCR 執行結果

| OCR 狀態 | 唯一資產數 |
|---|---:|
{table_counts(status_counts)}

解讀：

- `completed_cached`：之前已存在 OCR 快取，本次不用重跑，共 {len(completed_cached):,} 個。
- `completed`：這次在平台上新增 OCR 成功，共 {len(completed):,} 個。
- `error`：本次或狀態表中確認無法成功 OCR 的資產，共 {len(ocr_errors):,} 個。
- 可用 OCR Markdown 總資產數：{len(completed_any):,} 個。
- 完成狀態但 Markdown 檔不存在：{len(completed_missing_md):,} 個。

OCR 失敗完整清單：`{report_dir / 'ocr_error_assets.csv'}`

完成但找不到 Markdown 的清單：`{report_dir / 'completed_missing_markdown.csv'}`

### OCR 失敗原因分類

| errorCategory | 資產數 |
|---|---:|
{table_counts(ocr_error_counts)}

## 七、最終合併輸出

| 指標 | 數量 |
|---|---:|
| inline 合併輸出 md 檔案 | {file_count(inline_dir, '*.md'):,} |
| 有 inline OCR 區塊的 md | {inline_summary.get('mdWrittenWithOcr', 0):,} |
| inline OCR 區塊數 | {inline_summary.get('inlineOcrBlocksInserted', 0):,} |
| OCR 內容已內嵌 | {inline_summary.get('ocrAssetsAppended', 0):,} |
| 過大未內嵌、只保留 OCR Markdown 路徑 | {inline_summary.get('oversizedAssetsNotAppended', 0):,} |
| 找不到對應 relation index | {inline_summary.get('inlineRelationIndexesNotFound', 0):,} |

最終應使用：`{inline_dir}`

這版 OCR 區塊放在每個 `## 二、關聯的檔案資訊` 裡對應連結的正下方，不是集中放在檔案文末。

## 八、附錄 CSV

| 檔案 | 內容 |
|---|---|
| `md_without_related_links.csv` | 沒有掃到關聯連結的 Markdown |
| `unresolved_links.csv` | 所有解析不到/非本機檔案/格式異常的關聯連結 |
| `unresolved_ocr_supported_links.csv` | 解析不到，但副檔名原本可 OCR 的關聯連結 |
| `unresolved_not_ocr_supported_links.csv` | 解析不到，且不是 OCR 支援類型的關聯連結 |
| `not_ocr_supported_links.csv` | 所有不進 OCR 的關聯連結 |
| `resolved_not_ocr_supported_links.csv` | 已解析到本機，但不進 OCR 的關聯連結 |
| `ocr_error_assets.csv` | OCR 失敗的唯一資產 |
| `completed_missing_markdown.csv` | 完成狀態但 Markdown 不存在的異常清單，應為 0 |
"""
    (report_dir / "FINAL_REPORT.md").write_text(clean_text(report), encoding="utf-8", errors="replace")

    summary = {
        "generatedAt": datetime.now().isoformat(timespec="seconds"),
        "paths": {
            "sourceMdDir": str(source_md_dir),
            "manifest": str(manifest_path),
            "statusCsv": str(status_path),
            "ocrRunDir": "/mnt/project/output/ocr_run",
            "ocrMdAssets": "/mnt/project/output/ocr_run/ocr_md_assets",
            "cachedOcrMdAssets": "/mnt/project/cache/ocr_completed_cache/ocr_md_assets",
            "inlineMergedMdDir": str(inline_dir),
            "reportDir": str(report_dir),
        },
        "webMarkdown": {
            "total": len(md_rows),
            "withRelatedLinks": len(md_with_manifest_links),
            "withoutRelatedLinks": len(md_without_manifest_links),
        },
        "manifest": {
            "rows": len(manifest),
            "resolved": len(resolved),
            "unresolved": len(unresolved),
            "resolvedOcrSupported": len(resolved_ocr_supported),
            "resolvedNotOcrSupported": len(resolved_not_ocr_supported),
            "unresolvedOcrSupported": len(unresolved_ocr_supported),
            "unresolvedNotOcrSupported": len(unresolved_not_ocr_supported),
            "uniqueOcrCandidateAssets": len(unique_ocr_candidate_assets),
            "resolveStatusCounts": dict(resolve_counts),
        },
        "ocr": {
            "statusCounts": dict(status_counts),
            "completedCached": len(completed_cached),
            "completedNewOnPlatform": len(completed),
            "errors": len(ocr_errors),
            "usableOcrMarkdownAssets": len(completed_any),
            "completedMissingMarkdown": len(completed_missing_md),
            "errorCategories": dict(ocr_error_counts),
        },
        "merge": inline_summary,
    }
    (report_dir / "FINAL_REPORT_SUMMARY.json").write_text(
        json.dumps(clean_text(summary), ensure_ascii=False, indent=2),
        encoding="utf-8",
        errors="replace",
    )
    print(json.dumps(clean_text(summary), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
