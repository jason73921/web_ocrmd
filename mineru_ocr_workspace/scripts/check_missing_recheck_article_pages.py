#!/usr/bin/env python3
"""Check article page HTTP status for mdRelativePath entries in a manifest CSV."""

from __future__ import annotations

import argparse
import concurrent.futures
import csv
import socket
import ssl
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


WORKSPACE_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_INPUT = (
    WORKSPACE_ROOT
    / "mineru_ocr_workspace"
    / "output"
    / "manifest"
    / "missing_recheck"
    / "missing_recheck_confirmed_missing.csv"
)
DEFAULT_OUTPUT = (
    WORKSPACE_ROOT
    / "mineru_ocr_workspace"
    / "output"
    / "manifest"
    / "missing_recheck"
    / "missing_recheck_article_page_status.csv"
)
DEFAULT_MD_ROOT = WORKSPACE_ROOT / "\u7db2\u7ad9\u6587\u7ae0MD"
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0 Safari/537.36"
)

SOFT_404_PATTERNS = (
    "404",
    "not found",
    "page not found",
    "\u627e\u4e0d\u5230\u6b64\u9801",
    "\u9801\u9762\u4e0d\u5b58\u5728",
    "\u5f88\u62b1\u6b49",
)


@dataclass(frozen=True)
class PageRecord:
    index: int
    department: str
    md_relative_path: str
    article_url: str
    source_row_count: int
    first_input_row: int
    md_path: str
    md_exists: str


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Read missing_recheck_confirmed_missing.csv, de-duplicate "
            "mdRelativePath/articleUrl pairs, and check whether each article "
            "page loads, returns 404, or fails."
        )
    )
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--md-root", type=Path, default=DEFAULT_MD_ROOT)
    parser.add_argument("--timeout", type=float, default=15.0)
    parser.add_argument("--retries", type=int, default=1)
    parser.add_argument("--concurrency", type=int, default=12)
    parser.add_argument("--limit", type=int, default=0, help="Check only the first N unique pages.")
    parser.add_argument("--max-read-bytes", type=int, default=65536)
    parser.add_argument("--progress-every", type=int, default=25)
    parser.add_argument("--user-agent", default=DEFAULT_USER_AGENT)
    parser.add_argument(
        "--insecure",
        action="store_true",
        help="Disable TLS certificate verification for old or misconfigured sites.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Parse input and write rows without making HTTP requests.",
    )
    return parser.parse_args()


def read_unique_records(input_path: Path, md_root: Path) -> list[PageRecord]:
    grouped: dict[tuple[str, str], dict[str, object]] = {}

    with input_path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        missing = {"mdRelativePath", "articleUrl"} - set(reader.fieldnames or [])
        if missing:
            missing_list = ", ".join(sorted(missing))
            raise ValueError(f"Missing required CSV column(s): {missing_list}")

        for source_index, row in enumerate(reader, start=2):
            md_relative_path = (row.get("mdRelativePath") or "").strip()
            article_url = (row.get("articleUrl") or "").strip()
            department = (row.get("department") or "").strip()
            key = (md_relative_path, article_url)
            item = grouped.get(key)
            if item is None:
                grouped[key] = {
                    "department": department,
                    "row_count": 1,
                    "first_input_row": source_index,
                }
                continue

            item["row_count"] = int(item["row_count"]) + 1
            if department and department not in str(item["department"]).split("|"):
                item["department"] = f"{item['department']}|{department}" if item["department"] else department

    records: list[PageRecord] = []
    for index, ((md_relative_path, article_url), item) in enumerate(grouped.items(), start=1):
        md_path = resolve_md_path(md_root, md_relative_path)
        records.append(
            PageRecord(
                index=index,
                department=str(item["department"]),
                md_relative_path=md_relative_path,
                article_url=article_url,
                source_row_count=int(item["row_count"]),
                first_input_row=int(item["first_input_row"]),
                md_path=str(md_path) if md_path else "",
                md_exists=str(md_path.exists()).lower() if md_path else "false",
            )
        )

    return records


def resolve_md_path(md_root: Path, md_relative_path: str) -> Path | None:
    if not md_relative_path:
        return None
    normalized = Path(md_relative_path.replace("\\", "/"))
    if normalized.is_absolute() or any(part == ".." for part in normalized.parts):
        return None
    return md_root / normalized


def detect_charset(headers: object) -> str:
    get_content_charset = getattr(headers, "get_content_charset", None)
    if callable(get_content_charset):
        charset = get_content_charset()
        if charset:
            return charset
    return "utf-8"


def decode_snippet(raw: bytes, headers: object) -> str:
    charset = detect_charset(headers)
    try:
        return raw.decode(charset, errors="replace")
    except LookupError:
        return raw.decode("utf-8", errors="replace")


def looks_like_soft_404(text: str) -> bool:
    compact = " ".join(text.lower().split())
    if not compact:
        return False
    titleish_404 = "<title>404" in compact or "404 not found" in compact
    title_start = compact.find("<title>")
    title_end = compact.find("</title>", title_start)
    title = compact[title_start:title_end] if title_start >= 0 and title_end > title_start else ""
    phrase_match = any(pattern in title for pattern in SOFT_404_PATTERNS[1:])
    strong_body_match = any(
        pattern in compact
        for pattern in ("page not found", "\u627e\u4e0d\u5230\u6b64\u9801", "\u9801\u9762\u4e0d\u5b58\u5728")
    )
    return titleish_404 or phrase_match or strong_body_match


def classify_http_status(status_code: int, raw: bytes, text: str) -> str:
    if status_code == 404:
        return "404"
    if status_code >= 400:
        return "http_error"
    if status_code in (204, 205) or not raw:
        return "empty_response"
    if looks_like_soft_404(text):
        return "soft_404"
    return "loaded"


def check_page(
    record: PageRecord,
    timeout: float,
    retries: int,
    user_agent: str,
    max_read_bytes: int,
    ssl_context: ssl.SSLContext | None,
) -> dict[str, object]:
    if not record.article_url:
        return result_row(record, status="missing_url", error_type="missing_url")

    last_result: dict[str, object] | None = None
    attempts = max(1, retries + 1)
    for attempt in range(1, attempts + 1):
        last_result = fetch_page(
            record=record,
            timeout=timeout,
            attempt=attempt,
            user_agent=user_agent,
            max_read_bytes=max_read_bytes,
            ssl_context=ssl_context,
        )
        if last_result["status"] in {"loaded", "404", "soft_404", "http_error", "empty_response"}:
            return last_result

    assert last_result is not None
    return last_result


def fetch_page(
    record: PageRecord,
    timeout: float,
    attempt: int,
    user_agent: str,
    max_read_bytes: int,
    ssl_context: ssl.SSLContext | None,
) -> dict[str, object]:
    start = time.perf_counter()
    request = urllib.request.Request(
        record.article_url,
        headers={
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.8",
            "User-Agent": user_agent,
        },
        method="GET",
    )

    try:
        with urllib.request.urlopen(
            request,
            timeout=timeout,
            context=ssl_context,
        ) as response:
            raw = response.read(max_read_bytes + 1)
            snippet = decode_snippet(raw[:max_read_bytes], response.headers)
            status_code = int(getattr(response, "status", response.getcode()))
            return result_row(
                record,
                status=classify_http_status(status_code, raw, snippet),
                http_status=status_code,
                final_url=response.geturl(),
                content_type=response.headers.get("content-type", ""),
                content_length=response.headers.get("content-length", ""),
                bytes_read=min(len(raw), max_read_bytes),
                elapsed_ms=elapsed_ms(start),
                attempt=attempt,
            )
    except urllib.error.HTTPError as exc:
        raw = exc.read(max_read_bytes + 1)
        snippet = decode_snippet(raw[:max_read_bytes], exc.headers)
        return result_row(
            record,
            status=classify_http_status(exc.code, raw, snippet),
            http_status=exc.code,
            final_url=exc.geturl(),
            content_type=exc.headers.get("content-type", ""),
            content_length=exc.headers.get("content-length", ""),
            bytes_read=min(len(raw), max_read_bytes),
            elapsed_ms=elapsed_ms(start),
            attempt=attempt,
            error_type="HTTPError",
            error=str(exc),
        )
    except (urllib.error.URLError, TimeoutError, socket.timeout, OSError) as exc:
        return result_row(
            record,
            status="request_failed",
            elapsed_ms=elapsed_ms(start),
            attempt=attempt,
            error_type=type(exc).__name__,
            error=str(exc),
        )


def result_row(
    record: PageRecord,
    *,
    status: str,
    http_status: int | str = "",
    final_url: str = "",
    content_type: str = "",
    content_length: str = "",
    bytes_read: int | str = "",
    elapsed_ms: int | str = "",
    attempt: int | str = "",
    error_type: str = "",
    error: str = "",
) -> dict[str, object]:
    return {
        "department": record.department,
        "mdRelativePath": record.md_relative_path,
        "mdExists": record.md_exists,
        "mdPath": record.md_path,
        "articleUrl": record.article_url,
        "status": status,
        "httpStatus": http_status,
        "finalUrl": final_url,
        "contentType": content_type,
        "contentLength": content_length,
        "bytesRead": bytes_read,
        "elapsedMs": elapsed_ms,
        "attempt": attempt,
        "errorType": error_type,
        "error": error,
        "sourceRowCount": record.source_row_count,
        "firstInputRow": record.first_input_row,
        "_index": record.index,
    }


def elapsed_ms(start: float) -> int:
    return round((time.perf_counter() - start) * 1000)


def dry_run_row(record: PageRecord) -> dict[str, object]:
    return result_row(record, status="dry_run")


def write_report(output_path: Path, rows: Iterable[dict[str, object]]) -> None:
    fieldnames = [
        "department",
        "mdRelativePath",
        "mdExists",
        "mdPath",
        "articleUrl",
        "status",
        "httpStatus",
        "finalUrl",
        "contentType",
        "contentLength",
        "bytesRead",
        "elapsedMs",
        "attempt",
        "errorType",
        "error",
        "sourceRowCount",
        "firstInputRow",
    ]
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def print_summary(rows: list[dict[str, object]]) -> None:
    counts: dict[str, int] = {}
    for row in rows:
        status = str(row["status"])
        counts[status] = counts.get(status, 0) + 1
    summary = ", ".join(f"{key}={counts[key]}" for key in sorted(counts))
    print(f"Checked {len(rows)} unique page(s). {summary}")


def main() -> int:
    args = parse_args()
    if args.timeout <= 0:
        raise ValueError("--timeout must be greater than 0")
    if args.retries < 0:
        raise ValueError("--retries must be 0 or greater")
    if args.concurrency <= 0:
        raise ValueError("--concurrency must be greater than 0")
    if args.max_read_bytes <= 0:
        raise ValueError("--max-read-bytes must be greater than 0")

    records = read_unique_records(args.input, args.md_root)
    if args.limit > 0:
        records = records[: args.limit]

    if args.dry_run:
        rows = [dry_run_row(record) for record in records]
        write_report(args.output, rows)
        print_summary(rows)
        print(f"Wrote: {args.output}")
        return 0

    ssl_context = ssl._create_unverified_context() if args.insecure else None
    rows: list[dict[str, object]] = []
    completed = 0
    total = len(records)

    with concurrent.futures.ThreadPoolExecutor(max_workers=args.concurrency) as executor:
        futures = [
            executor.submit(
                check_page,
                record,
                args.timeout,
                args.retries,
                args.user_agent,
                args.max_read_bytes,
                ssl_context,
            )
            for record in records
        ]

        for future in concurrent.futures.as_completed(futures):
            rows.append(future.result())
            completed += 1
            if args.progress_every > 0 and (completed % args.progress_every == 0 or completed == total):
                print(f"Progress: {completed}/{total}", file=sys.stderr)

    rows.sort(key=lambda row: int(row["_index"]))
    write_report(args.output, rows)
    print_summary(rows)
    print(f"Wrote: {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
