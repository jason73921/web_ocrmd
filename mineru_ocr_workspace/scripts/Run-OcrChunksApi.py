#!/usr/bin/env python
"""Cross-platform chunk driver for MinerU FastAPI OCR runs.

The PowerShell chunk driver was written for the local Windows workspace. This
driver is intentionally plain Python so the same resumable flow can run on the
Linux AI-Stack host. It keeps one MinerU FastAPI server alive, runs chunks
through Invoke-OcrJobsApi.py, and falls back to table-disabled OCR when a native
table pipeline crash takes the API process down.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any


INFRA_ERROR_MARKERS = (
    "all connection attempts failed",
    "connection refused",
    "connection reset",
    "server disconnected",
    "remoteprotocolerror",
    "connecterror",
    "readerror",
    "peer closed",
    "broken pipe",
    "temporarily unavailable",
    "failed to establish a new connection",
    "winerror 10061",
)


def now_s() -> str:
    return datetime.now().replace(microsecond=0).isoformat(sep=" ")


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


def status_counts(path: Path) -> dict[str, int]:
    rows, _ = read_csv(path)
    counts: dict[str, int] = {}
    for row in rows:
        status = row.get("ocrStatus", "")
        counts[status] = counts.get(status, 0) + 1
    return counts


def counts_text(counts: dict[str, int]) -> str:
    return " ".join(f"{key}={counts[key]}" for key in sorted(counts))


def is_infra_error(text: str) -> bool:
    lowered = text.lower()
    return any(marker in lowered for marker in INFRA_ERROR_MARKERS)


def reset_retriable_errors(status_csv: Path) -> int:
    rows, fieldnames = read_csv(status_csv)
    changed = 0
    for row in rows:
        if row.get("ocrStatus") != "error":
            continue
        err = row.get("lastError", "")
        if not is_infra_error(err):
            continue
        row["ocrStatus"] = "pending"
        row["lastError"] = f"Reset retriable API infrastructure error: {err}"
        changed += 1
    if changed:
        write_csv(status_csv, rows, fieldnames)
    return changed


def reset_stale_running(status_csv: Path, max_attempts: int) -> tuple[int, int]:
    rows, fieldnames = read_csv(status_csv)
    reset = 0
    timed_out = 0
    for row in rows:
        if row.get("ocrStatus") != "running":
            continue
        try:
            attempts = int(row.get("attemptCount") or "0")
        except ValueError:
            attempts = 0
        if attempts >= max_attempts:
            row["ocrStatus"] = "timeout"
            row["lastError"] = f"Deferred stale running row after {attempts} attempts"
            timed_out += 1
        else:
            row["ocrStatus"] = "pending"
            row["lastError"] = "Reset stale running row before next chunk"
        reset += 1
    if reset:
        write_csv(status_csv, rows, fieldnames)
    return reset, timed_out


def select_pending_asset_ids(status_csv: Path, limit: int) -> list[str]:
    rows, _ = read_csv(status_csv)
    asset_ids: list[str] = []
    for row in rows:
        if len(asset_ids) >= limit:
            break
        if row.get("ocrStatus") != "pending":
            continue
        asset_id = row.get("assetId", "").strip()
        if asset_id:
            asset_ids.append(asset_id)
    return asset_ids


def collect_infra_failed_ids(status_csv: Path, candidate_ids: set[str]) -> list[str]:
    rows, _ = read_csv(status_csv)
    failed: list[str] = []
    for row in rows:
        asset_id = row.get("assetId", "")
        if asset_id not in candidate_ids:
            continue
        status = row.get("ocrStatus", "")
        if status == "running":
            failed.append(asset_id)
            continue
        if status in {"error", "timeout"} and is_infra_error(row.get("lastError", "")):
            failed.append(asset_id)
    return failed


def set_rows_pending(status_csv: Path, asset_ids: set[str], reason: str) -> int:
    rows, fieldnames = read_csv(status_csv)
    changed = 0
    for row in rows:
        if row.get("assetId") not in asset_ids:
            continue
        if row.get("ocrStatus", "").startswith("completed"):
            continue
        row["ocrStatus"] = "pending"
        row["lastError"] = reason
        changed += 1
    if changed:
        write_csv(status_csv, rows, fieldnames)
    return changed


def incomplete_ids(status_csv: Path, asset_ids: set[str]) -> list[str]:
    rows, _ = read_csv(status_csv)
    remaining: list[str] = []
    for row in rows:
        asset_id = row.get("assetId", "")
        if asset_id not in asset_ids:
            continue
        if not row.get("ocrStatus", "").startswith("completed"):
            remaining.append(asset_id)
    return remaining


def write_asset_ids(path: Path, asset_ids: list[str]) -> None:
    path.write_text("\n".join(asset_ids) + "\n", encoding="utf-8")


def free_tcp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def fetch_health(base_url: str, timeout_sec: float = 5.0) -> dict[str, Any] | None:
    try:
        with urllib.request.urlopen(f"{base_url}/health", timeout=timeout_sec) as response:
            body = response.read().decode("utf-8", errors="replace")
        return json.loads(body)
    except (OSError, urllib.error.URLError, json.JSONDecodeError):
        return None


def process_alive(process: subprocess.Popen[Any] | None) -> bool:
    return process is not None and process.poll() is None


def api_healthy(base_url: str | None, process: subprocess.Popen[Any] | None) -> bool:
    if not base_url or not process_alive(process):
        return False
    health = fetch_health(base_url)
    return bool(health and health.get("status") == "healthy")


def popen_kwargs() -> dict[str, Any]:
    if os.name == "nt":
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


def stop_process(process: subprocess.Popen[Any] | None, log) -> None:
    if not process_alive(process):
        return
    assert process is not None
    log(f"stopping process pid={process.pid}")
    try:
        if os.name == "nt":
            process.terminate()
        else:
            os.killpg(process.pid, signal.SIGTERM)
        process.wait(timeout=15)
        return
    except Exception:
        pass
    try:
        if os.name == "nt":
            process.kill()
        else:
            os.killpg(process.pid, signal.SIGKILL)
    except Exception:
        process.kill()
    try:
        process.wait(timeout=10)
    except Exception:
        pass


def start_api(args: argparse.Namespace, out_dir: Path, stamp: str, restart_index: int, log):
    port = args.api_port if args.api_port > 0 else free_tcp_port()
    base_url = f"http://127.0.0.1:{port}"
    api_out = out_dir / f"mineru_api_output_{stamp}_{restart_index:03d}"
    api_out.mkdir(parents=True, exist_ok=True)
    stdout_path = out_dir / f"mineru_api_{stamp}_{restart_index:03d}_stdout.log"
    stderr_path = out_dir / f"mineru_api_{stamp}_{restart_index:03d}_stderr.log"

    env = os.environ.copy()
    env["MINERU_API_OUTPUT_ROOT"] = str(api_out)
    env["MINERU_API_MAX_CONCURRENT_REQUESTS"] = str(args.api_max_concurrent_requests)
    env["MINERU_PROCESSING_WINDOW_SIZE"] = str(args.processing_window_size)
    env["MINERU_API_DISABLE_ACCESS_LOG"] = "1"
    env.pop("MINERU_API_SHUTDOWN_ON_STDIN_EOF", None)

    cmd = [
        sys.executable,
        "-m",
        "mineru.cli.fast_api",
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
    ]
    log(f"starting MinerU API restart={restart_index} url={base_url}")
    with stdout_path.open("wb") as stdout, stderr_path.open("wb") as stderr:
        process = subprocess.Popen(
            cmd,
            cwd=args.workspace_dir,
            env=env,
            stdout=stdout,
            stderr=stderr,
            **popen_kwargs(),
        )

    started = time.monotonic()
    while time.monotonic() - started < args.api_startup_timeout_sec:
        if process.poll() is not None:
            raise RuntimeError(
                f"MinerU API exited before healthy; pid={process.pid} exit={process.returncode} "
                f"stderr={stderr_path}"
            )
        health = fetch_health(base_url)
        if health and health.get("status") == "healthy":
            log(
                "MinerU API healthy "
                f"pid={process.pid} url={base_url} "
                f"maxConcurrent={health.get('max_concurrent_requests')} "
                f"window={health.get('processing_window_size')}"
            )
            return process, base_url
        time.sleep(2)
    stop_process(process, log)
    raise TimeoutError(f"Timed out waiting for MinerU API health: {base_url}")


def run_runner(
    args: argparse.Namespace,
    out_dir: Path,
    stamp: str,
    label: str,
    base_url: str,
    limit: int,
    batch_size: int,
    concurrency: int,
    disable_table: bool,
    only_asset_ids_file: Path | None,
    skip_error_retry: bool,
    skip_timeout_retry: bool,
    timeout_sec: int,
    log,
) -> int:
    stdout_path = out_dir / f"{label}_{stamp}_stdout.log"
    stderr_path = out_dir / f"{label}_{stamp}_stderr.log"
    cmd = [
        sys.executable,
        str(args.runner),
        "--jobs-csv",
        str(args.jobs_csv),
        "--out-dir",
        str(out_dir),
        "--backend",
        args.backend,
        "--language",
        args.language,
        "--method",
        args.method,
        "--limit",
        str(limit),
        "--timeout-sec",
        str(timeout_sec),
        "--concurrency",
        str(concurrency),
        "--batch-size",
        str(batch_size),
        "--retries",
        "0",
        "--retry-delay-sec",
        "2",
        "--api-url",
        base_url,
    ]
    if disable_table:
        cmd.append("--disable-table")
    if only_asset_ids_file is not None:
        cmd += ["--only-asset-ids-file", str(only_asset_ids_file)]
    if skip_error_retry:
        cmd.append("--skip-error-retry")
    if skip_timeout_retry:
        cmd.append("--skip-timeout-retry")

    log(
        f"running {label}: limit={limit} batch={batch_size} concurrency={concurrency} "
        f"disableTable={disable_table} stdout={stdout_path} stderr={stderr_path}"
    )
    started = time.monotonic()
    with stdout_path.open("wb") as stdout, stderr_path.open("wb") as stderr:
        process = subprocess.Popen(
            cmd,
            cwd=args.workspace_dir,
            stdout=stdout,
            stderr=stderr,
            **popen_kwargs(),
        )
        try:
            process.wait(timeout=args.chunk_timeout_sec)
        except subprocess.TimeoutExpired:
            stop_process(process, log)
            elapsed = time.monotonic() - started
            log(f"{label} timed out elapsedSec={elapsed:.2f}")
            return 124
    elapsed = time.monotonic() - started
    log(f"{label} exit={process.returncode} elapsedSec={elapsed:.2f}")
    return int(process.returncode or 0)


def parse_args() -> argparse.Namespace:
    script_dir = Path(__file__).resolve().parent
    workspace_dir = script_dir.parent
    parser = argparse.ArgumentParser(description="Run resumable MinerU OCR chunks through FastAPI.")
    parser.add_argument("--jobs-csv", required=True, type=Path)
    parser.add_argument("--out-dir", required=True, type=Path)
    parser.add_argument("--workspace-dir", type=Path, default=workspace_dir)
    parser.add_argument("--runner", type=Path, default=script_dir / "Invoke-OcrJobsApi.py")
    parser.add_argument("--chunk-limit", type=int, default=50)
    parser.add_argument("--max-chunks", type=int, default=1)
    parser.add_argument("--chunk-timeout-sec", type=int, default=1800)
    parser.add_argument("--api-startup-timeout-sec", type=int, default=600)
    parser.add_argument("--api-port", type=int, default=0)
    parser.add_argument("--api-max-concurrent-requests", type=int, default=1)
    parser.add_argument("--processing-window-size", type=int, default=16)
    parser.add_argument("--runner-concurrency", type=int, default=1)
    parser.add_argument("--batch-size", type=int, default=16)
    parser.add_argument("--fallback-batch-size", type=int, default=16)
    parser.add_argument("--fallback-concurrency", type=int, default=1)
    parser.add_argument("--timeout-sec", type=int, default=900)
    parser.add_argument("--fallback-timeout-sec", type=int, default=1800)
    parser.add_argument("--max-stale-running-attempts", type=int, default=3)
    parser.add_argument("--backend", default="pipeline")
    parser.add_argument("--language", default="chinese_cht")
    parser.add_argument("--method", default="auto", choices=["auto", "txt", "ocr"])
    parser.add_argument("--disable-table", action="store_true")
    parser.add_argument("--disable-fallback", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    args.jobs_csv = args.jobs_csv.resolve()
    args.out_dir = args.out_dir.resolve()
    args.workspace_dir = args.workspace_dir.resolve()
    args.runner = args.runner.resolve()

    if args.chunk_limit < 1:
        raise SystemExit("--chunk-limit must be >= 1")
    if args.max_chunks < 1:
        raise SystemExit("--max-chunks must be >= 1")
    if not args.jobs_csv.exists():
        raise FileNotFoundError(args.jobs_csv)
    if not args.runner.exists():
        raise FileNotFoundError(args.runner)

    args.out_dir.mkdir(parents=True, exist_ok=True)
    progress_log = args.out_dir / "ocr_api_chunk_driver.log"
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    def log(message: str) -> None:
        line = f"{now_s()} {message}"
        with progress_log.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
        print(line, flush=True)

    log(
        "OCR chunk driver start "
        f"jobsCsv={args.jobs_csv} outDir={args.out_dir} "
        f"chunkLimit={args.chunk_limit} maxChunks={args.max_chunks} "
        f"batchSize={args.batch_size} runnerConcurrency={args.runner_concurrency} "
        f"disableTable={args.disable_table}"
    )

    api_process: subprocess.Popen[Any] | None = None
    api_url: str | None = None
    api_restart = 0

    try:
        api_process, api_url = start_api(args, args.out_dir, stamp, api_restart, log)
        for chunk in range(1, args.max_chunks + 1):
            reset_running, stale_timeout = reset_stale_running(
                args.jobs_csv,
                args.max_stale_running_attempts,
            )
            reset_errors = reset_retriable_errors(args.jobs_csv)
            before = status_counts(args.jobs_csv)
            log(
                f"before chunk={chunk} {counts_text(before)} "
                f"resetRunning={reset_running} resetRetriable={reset_errors} "
                f"staleTimeout={stale_timeout}"
            )
            pending = before.get("pending", 0)
            running = before.get("running", 0)
            if pending <= 0 and running <= 0:
                log("no pending/running rows remain; stopping")
                break

            if not api_healthy(api_url, api_process):
                log(f"API unhealthy before chunk={chunk}; restarting")
                stop_process(api_process, log)
                api_restart += 1
                api_process, api_url = start_api(args, args.out_dir, stamp, api_restart, log)

            candidate_ids = select_pending_asset_ids(args.jobs_csv, args.chunk_limit)
            if not candidate_ids:
                log(f"chunk={chunk} has no pending candidates after reset; continuing")
                continue

            assert api_url is not None
            exit_code = run_runner(
                args=args,
                out_dir=args.out_dir,
                stamp=stamp,
                label=f"chunk_{chunk:03d}",
                base_url=api_url,
                limit=args.chunk_limit,
                batch_size=args.batch_size,
                concurrency=args.runner_concurrency,
                disable_table=args.disable_table,
                only_asset_ids_file=None,
                skip_error_retry=True,
                skip_timeout_retry=True,
                timeout_sec=args.timeout_sec,
                log=log,
            )

            healthy_after = api_healthy(api_url, api_process)
            fallback_ids: list[str] = []
            if not args.disable_fallback and (exit_code != 0 or not healthy_after):
                fallback_ids = collect_infra_failed_ids(args.jobs_csv, set(candidate_ids))

            if fallback_ids:
                log(
                    f"chunk={chunk} fallback candidates={len(fallback_ids)} "
                    "reason=API infrastructure failure; retrying with --disable-table"
                )
                stop_process(api_process, log)
                api_restart += 1
                api_process, api_url = start_api(args, args.out_dir, stamp, api_restart, log)
                set_rows_pending(
                    args.jobs_csv,
                    set(fallback_ids),
                    "Retrying after API native crash with table pipeline disabled",
                )
                ids_file = args.out_dir / f"fallback_asset_ids_{stamp}_{chunk:03d}.txt"
                write_asset_ids(ids_file, fallback_ids)
                exit_code = run_runner(
                    args=args,
                    out_dir=args.out_dir,
                    stamp=stamp,
                    label=f"chunk_{chunk:03d}_fallback_notable",
                    base_url=api_url,
                    limit=len(fallback_ids),
                    batch_size=args.fallback_batch_size,
                    concurrency=args.fallback_concurrency,
                    disable_table=True,
                    only_asset_ids_file=ids_file,
                    skip_error_retry=False,
                    skip_timeout_retry=False,
                    timeout_sec=args.fallback_timeout_sec,
                    log=log,
                )
                healthy_after = api_healthy(api_url, api_process)

                if exit_code != 0 or not healthy_after:
                    remaining = incomplete_ids(args.jobs_csv, set(fallback_ids))
                    if remaining:
                        log(
                            f"chunk={chunk} fallback batch still failed; "
                            f"retrying remaining={len(remaining)} one file per task"
                        )
                        stop_process(api_process, log)
                        api_restart += 1
                        api_process, api_url = start_api(
                            args,
                            args.out_dir,
                            stamp,
                            api_restart,
                            log,
                        )
                        set_rows_pending(
                            args.jobs_csv,
                            set(remaining),
                            "Retrying one-file table-disabled fallback after API crash",
                        )
                        ids_file = args.out_dir / f"fallback_single_asset_ids_{stamp}_{chunk:03d}.txt"
                        write_asset_ids(ids_file, remaining)
                        run_runner(
                            args=args,
                            out_dir=args.out_dir,
                            stamp=stamp,
                            label=f"chunk_{chunk:03d}_fallback_single",
                            base_url=api_url,
                            limit=len(remaining),
                            batch_size=1,
                            concurrency=1,
                            disable_table=True,
                            only_asset_ids_file=ids_file,
                            skip_error_retry=False,
                            skip_timeout_retry=False,
                            timeout_sec=args.fallback_timeout_sec,
                            log=log,
                        )

            if not api_healthy(api_url, api_process):
                log(f"API unhealthy after chunk={chunk}; restarting before next chunk")
                stop_process(api_process, log)
                api_restart += 1
                api_process, api_url = start_api(args, args.out_dir, stamp, api_restart, log)

            after = status_counts(args.jobs_csv)
            log(f"after chunk={chunk} {counts_text(after)}")

        final = status_counts(args.jobs_csv)
        log(f"final counts: {counts_text(final)}")
        return 0
    finally:
        stop_process(api_process, log)


if __name__ == "__main__":
    raise SystemExit(main())
