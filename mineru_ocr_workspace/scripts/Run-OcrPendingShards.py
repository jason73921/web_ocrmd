#!/usr/bin/env python
"""Run pending OCR rows with multiple independent MinerU API shard workers.

Each worker gets its own status CSV and output directory, so several MinerU API
processes can run at the same time without racing on the main CSV. After each
round, completed/error/timeout rows are merged back into the main status CSV.
"""

from __future__ import annotations

import argparse
import csv
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any


def now_s() -> str:
    return datetime.now().replace(microsecond=0).isoformat(sep=" ")


def read_csv(path: Path) -> tuple[list[dict[str, str]], list[str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        return [dict(row) for row in reader], list(reader.fieldnames or [])


def write_csv(path: Path, rows: list[dict[str, str]], fieldnames: list[str]) -> None:
    seen = set(fieldnames)
    for row in rows:
        for key in row:
            if key not in seen:
                fieldnames.append(key)
                seen.add(key)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    with tmp.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    tmp.replace(path)


def counts_text(rows: list[dict[str, str]]) -> str:
    counts: dict[str, int] = {}
    for row in rows:
        status = row.get("ocrStatus", "")
        counts[status] = counts.get(status, 0) + 1
    return " ".join(f"{key}={counts[key]}" for key in sorted(counts))


def split_evenly(rows: list[dict[str, str]], shard_count: int) -> list[list[dict[str, str]]]:
    shards = [[] for _ in range(shard_count)]
    for index, row in enumerate(rows):
        shards[index % shard_count].append(row)
    return [shard for shard in shards if shard]


def copy_markdown_to_main(row: dict[str, str], main_out_dir: Path) -> None:
    md_text = row.get("ocrMarkdownPath", "")
    if not md_text:
        return
    source = Path(md_text)
    if not source.exists():
        return
    target_dir = main_out_dir / "ocr_md_assets"
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / source.name
    if source.resolve() != target.resolve():
        shutil.copy2(source, target)
    row["ocrMarkdownPath"] = str(target)


def parse_args() -> argparse.Namespace:
    script_dir = Path(__file__).resolve().parent
    workspace_dir = script_dir.parent
    parser = argparse.ArgumentParser(description="Run pending OCR rows in parallel shards.")
    parser.add_argument("--jobs-csv", required=True, type=Path)
    parser.add_argument("--out-dir", required=True, type=Path)
    parser.add_argument("--shard-root", type=Path, default=None)
    parser.add_argument("--workspace-dir", type=Path, default=workspace_dir)
    parser.add_argument("--driver", type=Path, default=script_dir / "Run-OcrChunksApi.py")
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--rows-per-worker", type=int, default=100)
    parser.add_argument("--max-rounds", type=int, default=1000)
    parser.add_argument("--batch-size", type=int, default=32)
    parser.add_argument("--processing-window-size", type=int, default=32)
    parser.add_argument("--timeout-sec", type=int, default=1800)
    parser.add_argument("--chunk-timeout-sec", type=int, default=7200)
    parser.add_argument("--language", default="chinese_cht")
    parser.add_argument("--backend", default="pipeline")
    parser.add_argument("--method", default="auto", choices=["auto", "txt", "ocr"])
    parser.add_argument("--disable-table", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    args.jobs_csv = args.jobs_csv.resolve()
    args.out_dir = args.out_dir.resolve()
    args.workspace_dir = args.workspace_dir.resolve()
    args.driver = args.driver.resolve()
    if args.shard_root is None:
        args.shard_root = args.out_dir / f"parallel_shards_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    else:
        args.shard_root = args.shard_root.resolve()

    if args.workers < 1:
        raise SystemExit("--workers must be >= 1")
    if args.rows_per_worker < 1:
        raise SystemExit("--rows-per-worker must be >= 1")
    if not args.jobs_csv.exists():
        raise FileNotFoundError(args.jobs_csv)
    if not args.driver.exists():
        raise FileNotFoundError(args.driver)

    args.out_dir.mkdir(parents=True, exist_ok=True)
    args.shard_root.mkdir(parents=True, exist_ok=True)
    log_path = args.shard_root / "parallel_driver.log"

    def log(message: str) -> None:
        line = f"{now_s()} {message}"
        with log_path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
        print(line, flush=True)

    log(
        "parallel shard OCR start "
        f"jobsCsv={args.jobs_csv} outDir={args.out_dir} workers={args.workers} "
        f"rowsPerWorker={args.rows_per_worker} batchSize={args.batch_size} "
        f"window={args.processing_window_size} disableTable={args.disable_table}"
    )

    for round_index in range(1, args.max_rounds + 1):
        rows, fieldnames = read_csv(args.jobs_csv)
        pending = [row for row in rows if row.get("ocrStatus") == "pending"]
        log(f"round={round_index} before {counts_text(rows)}")
        if not pending:
            log("no pending rows remain; stopping")
            break

        selected = pending[: args.workers * args.rows_per_worker]
        if not selected:
            break
        selected_ids = {row.get("assetId", "") for row in selected}
        for row in rows:
            if row.get("assetId", "") in selected_ids:
                row["ocrStatus"] = "running"
                row["lastError"] = f"Claimed by parallel shard round={round_index}"
        write_csv(args.jobs_csv, rows, fieldnames)

        round_dir = args.shard_root / f"round_{round_index:04d}"
        round_dir.mkdir(parents=True, exist_ok=True)
        shard_rows = split_evenly(selected, min(args.workers, len(selected)))
        processes: list[tuple[int, Path, Path, subprocess.Popen[Any]]] = []

        for shard_index, shard in enumerate(shard_rows, start=1):
            shard_dir = round_dir / f"worker_{shard_index:02d}"
            shard_dir.mkdir(parents=True, exist_ok=True)
            shard_csv = shard_dir / "ocr_asset_status.csv"
            shard_for_csv = [dict(row, ocrStatus="pending", lastError="") for row in shard]
            write_csv(shard_csv, shard_for_csv, list(fieldnames))
            stdout_path = shard_dir / "driver.stdout.log"
            stderr_path = shard_dir / "driver.stderr.log"
            cmd = [
                sys.executable,
                str(args.driver),
                "--jobs-csv",
                str(shard_csv),
                "--out-dir",
                str(shard_dir),
                "--chunk-limit",
                str(len(shard)),
                "--max-chunks",
                "1",
                "--chunk-timeout-sec",
                str(args.chunk_timeout_sec),
                "--api-max-concurrent-requests",
                "1",
                "--processing-window-size",
                str(args.processing_window_size),
                "--runner-concurrency",
                "1",
                "--batch-size",
                str(args.batch_size),
                "--fallback-batch-size",
                str(args.batch_size),
                "--fallback-concurrency",
                "1",
                "--timeout-sec",
                str(args.timeout_sec),
                "--fallback-timeout-sec",
                str(max(args.timeout_sec, 2400)),
                "--backend",
                args.backend,
                "--language",
                args.language,
                "--method",
                args.method,
            ]
            if args.disable_table:
                cmd.append("--disable-table")
            log(f"round={round_index} start worker={shard_index} rows={len(shard)} dir={shard_dir}")
            with stdout_path.open("wb") as stdout, stderr_path.open("wb") as stderr:
                process = subprocess.Popen(
                    cmd,
                    cwd=args.workspace_dir,
                    stdout=stdout,
                    stderr=stderr,
                    start_new_session=(os.name != "nt"),
                )
            processes.append((shard_index, shard_csv, shard_dir, process))

        for shard_index, shard_csv, shard_dir, process in processes:
            process.wait()
            log(f"round={round_index} worker={shard_index} exit={process.returncode}")

        rows, fieldnames = read_csv(args.jobs_csv)
        by_id = {row.get("assetId", ""): row for row in rows}
        merged = 0
        for shard_index, shard_csv, shard_dir, process in processes:
            shard_status, _ = read_csv(shard_csv)
            for shard_row in shard_status:
                asset_id = shard_row.get("assetId", "")
                main_row = by_id.get(asset_id)
                if main_row is None:
                    continue
                status = shard_row.get("ocrStatus", "")
                if status.startswith("completed"):
                    copy_markdown_to_main(shard_row, args.out_dir)
                elif status in {"running", "pending", ""}:
                    status = "pending"
                    shard_row["lastError"] = (
                        f"Parallel shard did not finish round={round_index} worker={shard_index}"
                    )
                for key, value in shard_row.items():
                    main_row[key] = value
                main_row["ocrStatus"] = status
                merged += 1
        write_csv(args.jobs_csv, rows, fieldnames)
        log(f"round={round_index} merged={merged} after {counts_text(rows)}")

    rows, _ = read_csv(args.jobs_csv)
    log(f"final {counts_text(rows)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
