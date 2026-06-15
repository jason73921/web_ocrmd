#!/usr/bin/env python
"""Run OCR jobs by invoking the MinerU CLI once per asset.

This is the conservative fallback for environments where the long-lived
MinerU FastAPI server is unstable. It is slower than the API runner, but each
asset is isolated and the status CSV is updated after every file.
"""

from __future__ import annotations

import argparse
import csv
import os
import shutil
import subprocess
import time
from datetime import datetime
from pathlib import Path


STATUS_RUNNABLE = {"pending", "error", "timeout"}
DEFAULT_MAX_IMAGE_PIXELS = 50_000_000
DEFAULT_MAX_IMAGE_SIDE = 20_000
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}


def now_s() -> str:
    return datetime.now().replace(microsecond=0).isoformat()


def safe_asset_base_name(path_text: str) -> str:
    stem = Path(path_text).stem
    cleaned = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in stem)
    cleaned = cleaned[:80].strip("._-")
    return cleaned or "asset"


def read_csv(path: Path) -> tuple[list[dict[str, str]], list[str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        rows = [dict(row) for row in reader]
        fieldnames = list(reader.fieldnames or [])
    return rows, fieldnames


def write_csv(path: Path, rows: list[dict[str, str]], fieldnames: list[str]) -> None:
    seen = set(fieldnames)
    for row in rows:
        for key in row:
            if key not in seen:
                fieldnames.append(key)
                seen.add(key)

    tmp_path = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    with tmp_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    tmp_path.replace(path)


def ensure_fields(fieldnames: list[str], names: list[str]) -> list[str]:
    updated = list(fieldnames)
    existing = set(updated)
    for name in names:
        if name not in existing:
            updated.append(name)
            existing.add(name)
    return updated


def is_completed_cached(row: dict[str, str], force: bool) -> bool:
    if force:
        return False
    status = row.get("ocrStatus", "")
    md_path = row.get("ocrMarkdownPath", "")
    return status.startswith("completed") and bool(md_path) and Path(md_path).exists()


def should_process(
    row: dict[str, str],
    force: bool,
    skip_error_retry: bool,
    skip_timeout_retry: bool,
) -> bool:
    if is_completed_cached(row, force):
        return False
    if force:
        return True
    status = row.get("ocrStatus", "")
    if skip_error_retry and status == "error":
        return False
    if skip_timeout_retry and status == "timeout":
        return False
    return status in STATUS_RUNNABLE


def get_int_env(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None:
        return default
    try:
        resolved = int(value)
    except ValueError:
        return default
    return max(1, resolved)


def preflight_image_metadata(path: Path) -> str:
    try:
        from PIL import Image
    except Exception:
        return ""

    try:
        with Image.open(path) as image:
            width, height = image.size
    except Exception as exc:
        return f"Preflight unsupported file: image metadata could not be read: {exc}"

    if width <= 0 or height <= 0:
        return f"Preflight unsupported file: invalid image dimensions {width}x{height}"

    max_side = get_int_env("OCR_PREFLIGHT_MAX_IMAGE_SIDE", DEFAULT_MAX_IMAGE_SIDE)
    if width > max_side or height > max_side:
        return (
            "Preflight unsupported file: image side too large "
            f"{width}x{height}; limit={max_side}"
        )

    max_pixels = get_int_env("OCR_PREFLIGHT_MAX_IMAGE_PIXELS", DEFAULT_MAX_IMAGE_PIXELS)
    pixels = width * height
    if pixels > max_pixels:
        return (
            "Preflight unsupported file: image pixel count too large "
            f"{width}x{height}={pixels}; limit={max_pixels}"
        )

    return ""


def preflight_unsupported_reason(path: Path) -> str:
    if not path.exists():
        return f"Source file not found: {path}"
    try:
        header = path.read_bytes()[:512]
    except Exception as exc:
        return f"Could not read source file: {exc}"
    if not header:
        return "Preflight unsupported file: empty"

    ext = path.suffix.lower()
    stripped = header.lstrip()
    lower = stripped[:128].lower()
    if lower.startswith(b"<!doctype html") or lower.startswith(b"<html"):
        return "Preflight unsupported file: html content"
    if lower.startswith(b"<script") or lower.startswith(b"<?xml"):
        return "Preflight unsupported file: markup content"
    if header.startswith(b"8BPS"):
        return "Preflight unsupported file: psd content"

    if ext in {".jpg", ".jpeg"} and not header.startswith(b"\xff\xd8\xff"):
        return "Preflight unsupported file: jpeg extension with non-jpeg content"
    if ext == ".png" and not header.startswith(b"\x89PNG\r\n\x1a\n"):
        return "Preflight unsupported file: png extension with non-png content"
    if ext == ".webp" and not (header.startswith(b"RIFF") and header[8:12] == b"WEBP"):
        return "Preflight unsupported file: webp extension with non-webp content"
    if ext in IMAGE_EXTENSIONS:
        image_reason = preflight_image_metadata(path)
        if image_reason:
            return image_reason
    if ext == ".pdf" and not stripped.startswith(b"%PDF"):
        return "Preflight unsupported file: pdf extension with non-pdf content"
    if ext == ".pdf":
        try:
            import pypdfium2 as pdfium

            doc = pdfium.PdfDocument(str(path))
            try:
                _ = len(doc)
            finally:
                doc.close()
        except Exception as exc:
            return f"Preflight unsupported file: pdfium could not open PDF: {exc}"
    return ""


def pick_generated_markdown(asset_out_dir: Path) -> Path | None:
    candidates = [path for path in asset_out_dir.rglob("*.md") if path.is_file()]
    if not candidates:
        return None
    return max(candidates, key=lambda path: path.stat().st_size)


def run_mineru_cli(
    mineru_command: str,
    source_path: Path,
    asset_out_dir: Path,
    backend: str,
    language: str,
    timeout_sec: int,
    log_path: Path,
    err_path: Path,
    extra_args: list[str],
) -> subprocess.CompletedProcess[str]:
    command = [
        mineru_command,
        "-p",
        str(source_path),
        "-o",
        str(asset_out_dir),
        "-b",
        backend,
        "-l",
        language,
        *extra_args,
    ]
    with log_path.open("w", encoding="utf-8", errors="replace") as stdout_handle:
        with err_path.open("w", encoding="utf-8", errors="replace") as stderr_handle:
            return subprocess.run(
                command,
                stdout=stdout_handle,
                stderr=stderr_handle,
                text=True,
                timeout=timeout_sec,
                check=False,
            )


def main() -> int:
    parser = argparse.ArgumentParser(description="Run OCR jobs with per-file MinerU CLI invocations.")
    parser.add_argument("--jobs-csv", required=True)
    parser.add_argument("--out-dir", required=True)
    parser.add_argument("--mineru-command", default="mineru")
    parser.add_argument("--backend", default="pipeline")
    parser.add_argument("--language", default="chinese_cht")
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--timeout-sec", type=int, default=900)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--skip-error-retry", action="store_true")
    parser.add_argument("--skip-timeout-retry", action="store_true")
    parser.add_argument("--extra-mineru-arg", action="append", default=[])
    args = parser.parse_args()

    jobs_csv = Path(args.jobs_csv)
    out_dir = Path(args.out_dir)
    status_csv = out_dir / "ocr_asset_status.csv"
    asset_md_dir = out_dir / "ocr_md_assets"
    raw_dir = out_dir / "mineru_raw"
    summary_path = out_dir / "ocr_cli_run_summary.json"
    asset_md_dir.mkdir(parents=True, exist_ok=True)
    raw_dir.mkdir(parents=True, exist_ok=True)

    rows, fieldnames = read_csv(jobs_csv)
    fieldnames = ensure_fields(
        fieldnames,
        [
            "attemptCount",
            "ocrStatus",
            "lastError",
            "ocrMarkdownPath",
            "ocrGeneratedAt",
            "ocrChars",
            "ocrDurationSec",
            "needsReocr",
            "mineruTaskId",
        ],
    )

    selected_rows: list[dict[str, str]] = []
    preflight_errors = 0
    for row in rows:
        if len(selected_rows) >= args.limit:
            break
        if not should_process(
            row,
            force=args.force,
            skip_error_retry=args.skip_error_retry,
            skip_timeout_retry=args.skip_timeout_retry,
        ):
            continue
        source_path = Path(row.get("resolvedPath", ""))
        unsupported_reason = preflight_unsupported_reason(source_path)
        if unsupported_reason:
            row["attemptCount"] = str(int(row.get("attemptCount") or "0") + 1)
            row["ocrStatus"] = "error"
            row["lastError"] = unsupported_reason
            row["needsReocr"] = "Y"
            preflight_errors += 1
            continue
        selected_rows.append(row)

    write_csv(status_csv, rows, fieldnames)

    started_at = time.monotonic()
    success = 0
    errors = 0
    timeouts = 0

    for index, row in enumerate(selected_rows, start=1):
        asset_id = row["assetId"]
        source_path = Path(row["resolvedPath"])
        asset_out_dir = raw_dir / asset_id
        if asset_out_dir.exists():
            shutil.rmtree(asset_out_dir)
        asset_out_dir.mkdir(parents=True, exist_ok=True)
        safe_base = safe_asset_base_name(str(source_path))
        asset_md_path = asset_md_dir / f"{asset_id}_{safe_base}.md"
        log_path = asset_out_dir / "mineru.log"
        err_path = asset_out_dir / "mineru.err.log"

        row["attemptCount"] = str(int(row.get("attemptCount") or "0") + 1)
        row["ocrStatus"] = "running"
        row["lastError"] = ""
        write_csv(status_csv, rows, fieldnames)

        label = row.get("resolvedRelativePath") or str(source_path)
        print(f"[{index}/{len(selected_rows)}] OCR via CLI: {label}", flush=True)
        job_started = time.monotonic()
        try:
            completed = run_mineru_cli(
                mineru_command=args.mineru_command,
                source_path=source_path,
                asset_out_dir=asset_out_dir,
                backend=args.backend,
                language=args.language,
                timeout_sec=args.timeout_sec,
                log_path=log_path,
                err_path=err_path,
                extra_args=args.extra_mineru_arg,
            )
            if completed.returncode != 0:
                row["ocrStatus"] = "error"
                row["lastError"] = f"MinerU exited with code {completed.returncode}; stderr={err_path}"
                errors += 1
                continue

            generated = pick_generated_markdown(asset_out_dir)
            if generated is None:
                row["ocrStatus"] = "error"
                row["lastError"] = "MinerU did not produce a Markdown file"
                errors += 1
                continue

            shutil.copy2(generated, asset_md_path)
            content = asset_md_path.read_text(encoding="utf-8", errors="replace")
            row["ocrStatus"] = "completed"
            row["lastError"] = ""
            row["ocrMarkdownPath"] = str(asset_md_path)
            row["ocrGeneratedAt"] = now_s()
            row["ocrChars"] = str(len(content))
            row["needsReocr"] = "N"
            success += 1
        except subprocess.TimeoutExpired as exc:
            row["ocrStatus"] = "timeout"
            row["lastError"] = str(exc) or f"Timed out after {args.timeout_sec} seconds"
            timeouts += 1
        except Exception as exc:
            row["ocrStatus"] = "error"
            row["lastError"] = str(exc)
            errors += 1
        finally:
            row["ocrDurationSec"] = f"{time.monotonic() - job_started:.2f}"
            write_csv(status_csv, rows, fieldnames)

    elapsed = time.monotonic() - started_at
    summary = {
        "generatedAt": now_s(),
        "jobsCsv": str(jobs_csv),
        "outDir": str(out_dir),
        "mineruCommand": args.mineru_command,
        "backend": args.backend,
        "language": args.language,
        "limit": args.limit,
        "timeoutSec": args.timeout_sec,
        "selected": len(selected_rows),
        "preflightErrors": preflight_errors,
        "success": success,
        "errors": errors,
        "timeouts": timeouts,
        "elapsedSec": round(elapsed, 2),
        "statusCounts": {},
        "outputs": {
            "statusCsv": str(status_csv),
            "ocrMarkdownDir": str(asset_md_dir),
            "rawDir": str(raw_dir),
        },
    }
    for row in rows:
        status = row.get("ocrStatus", "")
        summary["statusCounts"][status] = summary["statusCounts"].get(status, 0) + 1
    summary_path.write_text(
        __import__("json").dumps(summary, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print("Done.", flush=True)
    print(f"Status: {status_csv}", flush=True)
    print(f"Summary: {summary_path}", flush=True)
    return 0 if errors == 0 and timeouts == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
