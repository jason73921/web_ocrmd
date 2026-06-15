#!/usr/bin/env python
"""Run OCR jobs through one long-lived MinerU FastAPI server.

This replaces the slow per-asset `mineru.exe -p <file>` loop. The old loop
starts a fresh local MinerU API/pipeline for every asset. This runner starts
the local API once per run, then submits each asset to `/tasks`.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import os
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx

from mineru.cli import api_client


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

    last_error: PermissionError | None = None
    for attempt in range(1, 31):
        tmp_path = path.with_name(f"{path.name}.{os.getpid()}.{attempt}.tmp")
        try:
            with tmp_path.open("w", encoding="utf-8-sig", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
                writer.writeheader()
                writer.writerows(rows)
            tmp_path.replace(path)
            return
        except PermissionError as exc:
            last_error = exc
            try:
                if tmp_path.exists():
                    tmp_path.unlink()
            except OSError:
                pass
            time.sleep(min(0.25 * attempt, 2.0))

    raise PermissionError(
        f"Could not replace status CSV after retries: {path}. "
        "Close Excel/file preview or any process that is locking the CSV."
    ) from last_error


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
    skip_error_retry: bool = False,
    skip_timeout_retry: bool = False,
) -> bool:
    if is_completed_cached(row, force):
        return False
    if force:
        return True
    if skip_error_retry and row.get("ocrStatus", "") == "error":
        return False
    if skip_timeout_retry and row.get("ocrStatus", "") == "timeout":
        return False
    return row.get("ocrStatus", "") in STATUS_RUNNABLE


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
    try:
        from mineru.cli.common import image_suffixes, office_suffixes, pdf_suffixes
        from mineru.utils.guess_suffix_or_lang import guess_suffix_by_path

        detected_suffix = guess_suffix_by_path(path)
        supported_suffixes = set(pdf_suffixes + image_suffixes + office_suffixes)
        if detected_suffix not in supported_suffixes:
            return (
                "Preflight unsupported file: MinerU detected unsupported file type: "
                f"{detected_suffix}"
            )
    except Exception as exc:
        return f"Preflight unsupported file: MinerU file type detection failed: {exc}"
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


def chunk_rows(rows: list[dict[str, str]], batch_size: int) -> list[list[dict[str, str]]]:
    resolved_batch_size = max(1, batch_size)
    return [
        rows[index : index + resolved_batch_size]
        for index in range(0, len(rows), resolved_batch_size)
    ]


def read_asset_id_filter(path_text: str | None) -> set[str] | None:
    if not path_text:
        return None
    path = Path(path_text)
    asset_ids = {
        line.strip()
        for line in path.read_text(encoding="utf-8-sig").splitlines()
        if line.strip()
    }
    return asset_ids


def markdown_for_asset(raw_asset_dir: Path, asset_id: str) -> Path | None:
    candidates = [
        path
        for path in raw_asset_dir.rglob("*.md")
        if path.is_file() and path.stem.startswith(f"{asset_id}_")
    ]
    if not candidates:
        candidates = [
            path
            for path in raw_asset_dir.rglob("*.md")
            if path.is_file() and f"{asset_id}_" in path.stem
        ]
    if not candidates:
        return None
    return max(candidates, key=lambda path: path.stat().st_size)


def copy_asset_raw_from_batch(batch_out_dir: Path, raw_dir: Path, row: dict[str, str]) -> Path:
    asset_id = row["assetId"]
    asset_out_dir = raw_dir / asset_id
    if asset_out_dir.exists():
        shutil.rmtree(asset_out_dir)
    asset_out_dir.mkdir(parents=True, exist_ok=True)

    matched_roots = [
        path
        for path in batch_out_dir.iterdir()
        if path.is_dir() and path.name.startswith(f"{asset_id}_")
    ]
    if matched_roots:
        for matched_root in matched_roots:
            shutil.copytree(matched_root, asset_out_dir / matched_root.name)
    else:
        # Fallback for unexpected ZIP layouts: keep the full batch output under
        # the asset raw directory so the generated Markdown is still searchable.
        shutil.copytree(batch_out_dir, asset_out_dir / "_batch_extract")
    return asset_out_dir


def build_form_data(args: argparse.Namespace) -> dict[str, str | list[str]]:
    return api_client.build_parse_request_form_data(
        lang_list=[args.language],
        backend=args.backend,
        parse_method=args.method,
        formula_enable=not args.disable_formula,
        table_enable=not args.disable_table,
        server_url=args.server_url,
        start_page_id=args.start,
        end_page_id=args.end,
        image_analysis=not args.disable_image_analysis,
        return_md=True,
        return_middle_json=True,
        return_model_output=True,
        return_content_list=True,
        return_images=True,
        response_format_zip=True,
        return_original_file=True,
    )


async def ensure_server(
    args: argparse.Namespace,
    http_client: httpx.AsyncClient,
) -> tuple[str, api_client.LocalAPIServer | None, dict[str, Any]]:
    if args.api_url:
        base_url = api_client.normalize_base_url(args.api_url)
        health = await api_client.fetch_server_health(http_client, base_url)
        return base_url, None, {
            "mode": "external_api_url",
            "baseUrl": base_url,
            "maxConcurrentRequests": health.max_concurrent_requests,
            "processingWindowSize": health.processing_window_size,
            "localServerStarted": False,
        }

    local_server = api_client.LocalAPIServer(extra_cli_args=tuple(args.extra_api_arg))
    base_url = local_server.start()
    health = await api_client.wait_for_local_api_ready(http_client, local_server)
    return base_url, local_server, {
        "mode": "single_local_api_server",
        "baseUrl": base_url,
        "maxConcurrentRequests": health.max_concurrent_requests,
        "processingWindowSize": health.processing_window_size,
        "localServerStarted": True,
    }


async def run_one_job(
    row: dict[str, str],
    args: argparse.Namespace,
    http_client: httpx.AsyncClient,
    base_url: str,
    form_data: dict[str, str | list[str]],
    asset_md_dir: Path,
    raw_dir: Path,
) -> str:
    source_path = Path(row.get("resolvedPath", ""))
    if not source_path.exists():
        raise FileNotFoundError(f"Source file not found: {source_path}")

    asset_id = row["assetId"]
    asset_out_dir = raw_dir / asset_id
    asset_out_dir.mkdir(parents=True, exist_ok=True)
    asset_md_path = asset_md_dir / f"{asset_id}_{safe_asset_base_name(str(source_path))}.md"

    upload_name = f"{asset_id}_{safe_asset_base_name(str(source_path))}{source_path.suffix}"
    upload_assets = [
        api_client.UploadAsset(path=source_path, upload_name=upload_name),
    ]
    submit_response = await api_client.submit_parse_task(
        base_url=base_url,
        upload_assets=upload_assets,
        form_data=form_data,
    )
    row["mineruTaskId"] = submit_response.task_id

    await api_client.wait_for_task_result(
        client=http_client,
        submit_response=submit_response,
        task_label=row.get("resolvedRelativePath") or str(source_path),
        timeout_seconds=args.timeout_sec,
    )
    zip_path = await api_client.download_result_zip(
        client=http_client,
        submit_response=submit_response,
        task_label=row.get("resolvedRelativePath") or str(source_path),
    )
    try:
        api_client.safe_extract_zip(zip_path, asset_out_dir)
    finally:
        zip_path.unlink(missing_ok=True)

    generated = pick_generated_markdown(asset_out_dir)
    if generated is None:
        raise RuntimeError("MinerU did not produce a Markdown file")

    shutil.copy2(generated, asset_md_path)
    content = asset_md_path.read_text(encoding="utf-8", errors="replace")
    row["ocrMarkdownPath"] = str(asset_md_path)
    row["ocrGeneratedAt"] = now_s()
    row["ocrChars"] = str(len(content))
    row["needsReocr"] = "N"
    return str(asset_md_path)


async def run_job_batch(
    batch_rows: list[dict[str, str]],
    args: argparse.Namespace,
    http_client: httpx.AsyncClient,
    base_url: str,
    form_data: dict[str, str | list[str]],
    asset_md_dir: Path,
    raw_dir: Path,
    batch_raw_dir: Path,
    batch_index: int,
) -> None:
    upload_assets: list[api_client.UploadAsset] = []
    labels: list[str] = []
    for row in batch_rows:
        source_path = Path(row.get("resolvedPath", ""))
        if not source_path.exists():
            raise FileNotFoundError(f"Source file not found: {source_path}")
        asset_id = row["assetId"]
        upload_name = f"{asset_id}_{safe_asset_base_name(str(source_path))}{source_path.suffix}"
        upload_assets.append(api_client.UploadAsset(path=source_path, upload_name=upload_name))
        labels.append(row.get("resolvedRelativePath") or str(source_path))

    first_asset_id = batch_rows[0]["assetId"]
    batch_out_dir = batch_raw_dir / f"batch_{batch_index:05d}_{first_asset_id}_{len(batch_rows)}"
    if batch_out_dir.exists():
        shutil.rmtree(batch_out_dir)
    batch_out_dir.mkdir(parents=True, exist_ok=True)

    submit_response = await api_client.submit_parse_task(
        base_url=base_url,
        upload_assets=upload_assets,
        form_data=form_data,
    )
    for row in batch_rows:
        row["mineruTaskId"] = submit_response.task_id

    task_label = f"batch#{batch_index} ({len(batch_rows)} files): {labels[0]}"
    await api_client.wait_for_task_result(
        client=http_client,
        submit_response=submit_response,
        task_label=task_label,
        timeout_seconds=args.timeout_sec,
    )
    zip_path = await api_client.download_result_zip(
        client=http_client,
        submit_response=submit_response,
        task_label=task_label,
    )
    try:
        api_client.safe_extract_zip(zip_path, batch_out_dir)
    finally:
        zip_path.unlink(missing_ok=True)

    for row in batch_rows:
        source_path = Path(row.get("resolvedPath", ""))
        asset_id = row["assetId"]
        asset_out_dir = copy_asset_raw_from_batch(batch_out_dir, raw_dir, row)
        generated = markdown_for_asset(asset_out_dir, asset_id)
        if generated is None:
            raise RuntimeError(
                f"MinerU did not produce a Markdown file for {asset_id}: "
                f"{row.get('resolvedRelativePath')}"
            )
        asset_md_path = asset_md_dir / f"{asset_id}_{safe_asset_base_name(str(source_path))}.md"
        shutil.copy2(generated, asset_md_path)
        content = asset_md_path.read_text(encoding="utf-8", errors="replace")
        row["ocrMarkdownPath"] = str(asset_md_path)
        row["ocrGeneratedAt"] = now_s()
        row["ocrChars"] = str(len(content))
        row["needsReocr"] = "N"


async def run(args: argparse.Namespace) -> int:
    jobs_path = Path(args.jobs_csv).resolve()
    out_dir = Path(args.out_dir).resolve()
    status_csv = out_dir / "ocr_asset_status.csv"
    asset_md_dir = out_dir / "ocr_md_assets"
    raw_dir = out_dir / "mineru_raw"
    batch_raw_dir = raw_dir / "_batches"
    summary_path = out_dir / "ocr_api_run_summary.json"

    if not jobs_path.exists():
        raise FileNotFoundError(f"JobsCsv not found: {jobs_path}")

    out_dir.mkdir(parents=True, exist_ok=True)
    asset_md_dir.mkdir(parents=True, exist_ok=True)
    raw_dir.mkdir(parents=True, exist_ok=True)
    batch_raw_dir.mkdir(parents=True, exist_ok=True)

    rows, fieldnames = read_csv(jobs_path)
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

    started_at = time.monotonic()
    started_wall = now_s()
    skipped_cached = 0
    preflight_errors = 0
    selected_rows: list[dict[str, str]] = []
    success = 0
    errors = 0
    timeouts = 0
    server_info: dict[str, Any] = {"mode": "not_started", "localServerStarted": False}
    asset_id_filter = read_asset_id_filter(args.only_asset_ids_file)

    form_data = build_form_data(args)
    local_server: api_client.LocalAPIServer | None = None
    timeout = api_client.build_http_timeout()

    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as http_client:
        try:
            for row in rows:
                if len(selected_rows) >= args.limit:
                    break
                if asset_id_filter is not None and row.get("assetId", "") not in asset_id_filter:
                    continue
                if is_completed_cached(row, args.force):
                    skipped_cached += 1
                    continue
                if not should_process(
                    row,
                    args.force,
                    args.skip_error_retry,
                    args.skip_timeout_retry,
                ):
                    continue

                unsupported_reason = preflight_unsupported_reason(Path(row.get("resolvedPath", "")))
                if unsupported_reason:
                    row["attemptCount"] = str(int(row.get("attemptCount") or "0") + 1)
                    row["ocrStatus"] = "error"
                    row["lastError"] = unsupported_reason
                    row["needsReocr"] = "Y"
                    preflight_errors += 1
                    continue

                selected_rows.append(row)

            if selected_rows:
                base_url, local_server, server_info = await ensure_server(args, http_client)
                effective_concurrency = args.concurrency
                if effective_concurrency < 1:
                    effective_concurrency = int(server_info.get("maxConcurrentRequests") or 1)
                batches = chunk_rows(selected_rows, args.batch_size)
                effective_concurrency = max(1, min(effective_concurrency, len(batches)))
                server_info["runnerConcurrency"] = effective_concurrency
                server_info["runnerBatchSize"] = max(1, args.batch_size)
                server_info["runnerBatchCount"] = len(batches)
            else:
                base_url = ""
                effective_concurrency = 1
                batches = []

            for row in selected_rows:
                row["attemptCount"] = str(int(row.get("attemptCount") or "0") + 1)
                row["ocrStatus"] = "running"
                row["lastError"] = ""
            write_csv(status_csv, rows, fieldnames)

            queue: asyncio.Queue[tuple[int, list[dict[str, str]]] | None] = asyncio.Queue()
            for index, batch_rows in enumerate(batches, start=1):
                queue.put_nowait((index, batch_rows))
            for _ in range(effective_concurrency):
                queue.put_nowait(None)

            csv_lock = asyncio.Lock()
            counter_lock = asyncio.Lock()

            async def worker() -> None:
                nonlocal success, errors, timeouts
                while True:
                    item = await queue.get()
                    try:
                        if item is None:
                            return
                        index, batch_rows = item
                        label = (
                            batch_rows[0].get("resolvedRelativePath")
                            or batch_rows[0].get("resolvedPath")
                            or batch_rows[0].get("assetId")
                        )
                        print(
                            f"[batch {index}/{len(batches)}] OCR via API "
                            f"(concurrency {effective_concurrency}, size {len(batch_rows)}): {label}",
                            flush=True,
                        )

                        job_started = time.monotonic()
                        try:
                            last_exc: Exception | None = None
                            for attempt in range(args.retries + 1):
                                try:
                                    await run_job_batch(
                                        batch_rows=batch_rows,
                                        args=args,
                                        http_client=http_client,
                                        base_url=base_url,
                                        form_data=form_data,
                                        asset_md_dir=asset_md_dir,
                                        raw_dir=raw_dir,
                                        batch_raw_dir=batch_raw_dir,
                                        batch_index=index,
                                    )
                                    last_exc = None
                                    break
                                except Exception as exc:
                                    last_exc = exc
                                    if attempt >= args.retries:
                                        break
                                    for row in batch_rows:
                                        row["lastError"] = (
                                            f"Retry {attempt + 1}/{args.retries} after: {exc}"
                                        )
                                    async with csv_lock:
                                        write_csv(status_csv, rows, fieldnames)
                                    await asyncio.sleep(args.retry_delay_sec)
                            if last_exc is not None:
                                if len(batch_rows) == 1:
                                    raise last_exc
                                print(
                                    f"[batch {index}/{len(batches)}] batch failed; "
                                    "falling back to per-file retry for this batch",
                                    flush=True,
                                )
                                split_success = 0
                                split_errors = 0
                                split_timeouts = 0
                                for split_offset, single_row in enumerate(batch_rows, start=1):
                                    single_last_exc: Exception | None = None
                                    try:
                                        for attempt in range(args.retries + 1):
                                            try:
                                                await run_job_batch(
                                                    batch_rows=[single_row],
                                                    args=args,
                                                    http_client=http_client,
                                                    base_url=base_url,
                                                    form_data=form_data,
                                                    asset_md_dir=asset_md_dir,
                                                    raw_dir=raw_dir,
                                                    batch_raw_dir=batch_raw_dir,
                                                    batch_index=index * 1000 + split_offset,
                                                )
                                                single_last_exc = None
                                                break
                                            except Exception as exc:
                                                single_last_exc = exc
                                                if attempt >= args.retries:
                                                    break
                                                single_row["lastError"] = (
                                                    f"Retry {attempt + 1}/{args.retries} after: {exc}"
                                                )
                                                async with csv_lock:
                                                    write_csv(status_csv, rows, fieldnames)
                                                await asyncio.sleep(args.retry_delay_sec)
                                        if single_last_exc is not None:
                                            raise single_last_exc
                                        single_row["ocrStatus"] = "completed"
                                        single_row["lastError"] = ""
                                        split_success += 1
                                    except asyncio.TimeoutError as exc:
                                        single_row["ocrStatus"] = "timeout"
                                        single_row["lastError"] = (
                                            str(exc) or f"Timed out after {args.timeout_sec} seconds"
                                        )
                                        split_timeouts += 1
                                    except Exception as exc:
                                        single_row["ocrStatus"] = "error"
                                        single_row["lastError"] = str(exc)
                                        split_errors += 1
                                    finally:
                                        async with csv_lock:
                                            write_csv(status_csv, rows, fieldnames)
                                async with counter_lock:
                                    success += split_success
                                    errors += split_errors
                                    timeouts += split_timeouts
                            else:
                                for row in batch_rows:
                                    row["ocrStatus"] = "completed"
                                async with counter_lock:
                                    success += len(batch_rows)
                        except asyncio.TimeoutError as exc:
                            for row in batch_rows:
                                row["ocrStatus"] = "timeout"
                                row["lastError"] = str(exc) or f"Timed out after {args.timeout_sec} seconds"
                            async with counter_lock:
                                timeouts += len(batch_rows)
                        except Exception as exc:
                            for row in batch_rows:
                                row["ocrStatus"] = "error"
                                row["lastError"] = str(exc)
                            async with counter_lock:
                                errors += len(batch_rows)
                        finally:
                            duration = f"{time.monotonic() - job_started:.2f}"
                            for row in batch_rows:
                                row["ocrDurationSec"] = duration
                            async with csv_lock:
                                write_csv(status_csv, rows, fieldnames)
                    finally:
                        queue.task_done()

            workers = [asyncio.create_task(worker()) for _ in range(effective_concurrency)]
            await queue.join()
            await asyncio.gather(*workers, return_exceptions=True)
        finally:
            pass

    elapsed = time.monotonic() - started_at
    summary = {
        "generatedAt": now_s(),
        "startedAt": started_wall,
        "jobsCsv": str(jobs_path),
        "outDir": str(out_dir),
        "backend": args.backend,
        "language": args.language,
        "method": args.method,
        "limit": args.limit,
        "onlyAssetIdsFile": str(args.only_asset_ids_file) if args.only_asset_ids_file else None,
        "timeoutSec": args.timeout_sec,
        "processingMode": "single long-lived MinerU API server; no per-file mineru.exe restart",
        "batchSize": max(1, args.batch_size),
        "server": server_info,
        "processedThisRun": len(selected_rows),
        "skippedCachedThisRun": skipped_cached,
        "preflightErrorsThisRun": preflight_errors,
        "successThisRun": success,
        "errorsThisRun": errors,
        "timeoutsThisRun": timeouts,
        "totalCompleted": sum(1 for row in rows if row.get("ocrStatus", "").startswith("completed")),
        "totalPending": sum(1 for row in rows if row.get("ocrStatus") == "pending"),
        "totalError": sum(1 for row in rows if row.get("ocrStatus") == "error"),
        "totalTimeout": sum(1 for row in rows if row.get("ocrStatus") == "timeout"),
        "elapsedSec": round(elapsed, 2),
        "outputs": {
            "statusCsv": str(status_csv),
            "ocrMarkdownDir": str(asset_md_dir),
            "rawDir": str(raw_dir),
        },
    }
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print("Done.", flush=True)
    print(f"Status: {status_csv}", flush=True)
    print(f"Summary: {summary_path}", flush=True)
    if local_server is not None:
        local_server.stop()
    return 0 if errors == 0 and timeouts == 0 else 1


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run OCR jobs with one reusable MinerU API server."
    )
    parser.add_argument("--jobs-csv", required=True)
    parser.add_argument("--out-dir", required=True)
    parser.add_argument("--backend", default="pipeline")
    parser.add_argument("--language", default="chinese_cht")
    parser.add_argument("--method", default="auto", choices=["auto", "txt", "ocr"])
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--timeout-sec", type=float, default=900)
    parser.add_argument(
        "--concurrency",
        type=int,
        default=0,
        help="Concurrent MinerU API tasks. 0 means use server maxConcurrentRequests.",
    )
    parser.add_argument("--batch-size", type=int, default=1)
    parser.add_argument("--retries", type=int, default=2)
    parser.add_argument("--retry-delay-sec", type=float, default=2.0)
    parser.add_argument("--api-url", default=None, help="Use an already-running MinerU API.")
    parser.add_argument("--server-url", default=None)
    parser.add_argument(
        "--only-asset-ids-file",
        default=None,
        help="Process only assetId values listed one per line in this file.",
    )
    parser.add_argument("--start", type=int, default=0)
    parser.add_argument("--end", type=int, default=None)
    parser.add_argument("--disable-formula", action="store_true")
    parser.add_argument("--disable-table", action="store_true")
    parser.add_argument("--disable-image-analysis", action="store_true")
    parser.add_argument("--extra-api-arg", action="append", default=[])
    parser.add_argument("--force", action="store_true")
    parser.add_argument(
        "--skip-error-retry",
        action="store_true",
        help="Do not pick rows already marked error; useful for chunked continuation.",
    )
    parser.add_argument(
        "--skip-timeout-retry",
        action="store_true",
        help="Do not pick rows already marked timeout; useful for isolating slow/stuck files.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.limit < 1:
        raise SystemExit("--limit must be >= 1")
    return asyncio.run(run(args))


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)
