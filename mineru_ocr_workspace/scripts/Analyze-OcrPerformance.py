#!/usr/bin/env python
"""Analyze MinerU OCR throughput from the status CSV and chunk progress log."""

from __future__ import annotations

import argparse
import csv
import json
import re
import statistics
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any


START_RE = re.compile(r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) chunked OCR start;")
BEFORE_RE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) before chunk=(?P<chunk>\d+) "
    r"pending=(?P<pending>\d+) running=(?P<running>\d+) "
    r"resetRunning=(?P<reset_running>\d+) resetRetriable=(?P<reset_retriable>\d+)"
)
AFTER_RE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) after chunk=(?P<chunk>\d+) "
    r"exit=(?P<exit>\S*) elapsedSec=(?P<elapsed>[\d.]+)"
)
TIMEOUT_RE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) chunk=(?P<chunk>\d+) exceeded timeoutSec=(?P<timeout>\d+)"
)
FINAL_COUNTS_RE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) final counts: (?P<counts>.*)$"
)


def parse_dt(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%d %H:%M:%S")


def as_int(value: str | None, default: int = 0) -> int:
    try:
        return int(float(value or ""))
    except Exception:
        return default


def as_float(value: str | None, default: float = 0.0) -> float:
    try:
        return float(value or "")
    except Exception:
        return default


def percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = (len(ordered) - 1) * pct
    lower = int(index)
    upper = min(lower + 1, len(ordered) - 1)
    weight = index - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def size_summary(rows: list[dict[str, str]]) -> dict[str, Any]:
    sizes = [as_int(row.get("sourceBytes")) for row in rows if as_int(row.get("sourceBytes")) > 0]
    if not sizes:
        return {"count": 0}
    return {
        "count": len(sizes),
        "totalMB": round(sum(sizes) / 1024 / 1024, 2),
        "avgKB": round(statistics.mean(sizes) / 1024, 2),
        "medianKB": round(statistics.median(sizes) / 1024, 2),
        "p90KB": round(percentile([float(v) for v in sizes], 0.90) / 1024, 2),
        "maxMB": round(max(sizes) / 1024 / 1024, 2),
    }


def read_status(path: Path) -> tuple[list[dict[str, str]], list[str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        return [dict(row) for row in reader], list(reader.fieldnames or [])


def parse_progress_log(path: Path) -> list[dict[str, Any]]:
    runs: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    if not path.exists():
        return runs

    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        start = START_RE.search(line)
        if start:
            current = {
                "startedAt": start.group("ts"),
                "chunks": {},
                "timeouts": set(),
            }
            runs.append(current)
            continue
        if current is None:
            current = {"startedAt": "unknown", "chunks": {}, "timeouts": set()}
            runs.append(current)

        before = BEFORE_RE.search(line)
        if before:
            chunk = int(before.group("chunk"))
            record = current["chunks"].setdefault(chunk, {})
            record["beforeAt"] = before.group("ts")
            record["pendingBefore"] = int(before.group("pending"))
            record["runningBefore"] = int(before.group("running"))
            record["resetRunning"] = int(before.group("reset_running"))
            record["resetRetriable"] = int(before.group("reset_retriable"))
            continue

        after = AFTER_RE.search(line)
        if after:
            chunk = int(after.group("chunk"))
            record = current["chunks"].setdefault(chunk, {})
            record["afterAt"] = after.group("ts")
            record["exit"] = after.group("exit")
            record["elapsedSec"] = float(after.group("elapsed"))
            continue

        timeout = TIMEOUT_RE.search(line)
        if timeout:
            current["timeouts"].add(int(timeout.group("chunk")))
            continue

        final_counts = FINAL_COUNTS_RE.search(line)
        if final_counts:
            counts: dict[str, int] = {}
            for part in final_counts.group("counts").split():
                if "=" not in part:
                    continue
                key, value = part.split("=", 1)
                counts[key] = as_int(value)
            current["finalCounts"] = counts
            current["finalAt"] = final_counts.group("ts")

    for run in runs:
        chunks = run["chunks"]
        for chunk, record in chunks.items():
            next_record = chunks.get(chunk + 1)
            before_pending = record.get("pendingBefore")
            next_pending = next_record.get("pendingBefore") if next_record else None
            if before_pending is not None and next_pending is not None:
                record["advanced"] = max(0, before_pending - next_pending)
            elif before_pending is not None and run.get("finalCounts", {}).get("pending") is not None:
                record["advanced"] = max(0, before_pending - int(run["finalCounts"]["pending"]))
            else:
                record["advanced"] = None
            record["timedOut"] = chunk in run["timeouts"]
    return runs


def summarize_run(run: dict[str, Any]) -> dict[str, Any]:
    chunks = [run["chunks"][key] for key in sorted(run["chunks"])]
    completed_chunks = [chunk for chunk in chunks if "elapsedSec" in chunk]
    elapsed = sum(float(chunk.get("elapsedSec", 0.0)) for chunk in completed_chunks)
    advanced = sum(int(chunk.get("advanced") or 0) for chunk in completed_chunks)
    zero_progress = [
        chunk
        for chunk in completed_chunks
        if int(chunk.get("advanced") or 0) == 0 and float(chunk.get("elapsedSec", 0.0)) >= 120
    ]
    productive = [
        chunk
        for chunk in completed_chunks
        if int(chunk.get("advanced") or 0) > 0
    ]
    return {
        "startedAt": run.get("startedAt"),
        "chunkCount": len(completed_chunks),
        "advancedFiles": advanced,
        "elapsedSec": round(elapsed, 2),
        "avgWallSecPerAdvancedFile": round(elapsed / advanced, 2) if advanced else None,
        "productiveChunkCount": len(productive),
        "zeroProgressChunkCount": len(zero_progress),
        "zeroProgressElapsedSec": round(sum(float(chunk.get("elapsedSec", 0.0)) for chunk in zero_progress), 2),
        "timeoutChunkCount": sum(1 for chunk in completed_chunks if chunk.get("timedOut")),
        "slowestChunks": sorted(
            [
                {
                    "chunk": index,
                    "elapsedSec": round(float(chunk.get("elapsedSec", 0.0)), 2),
                    "advanced": chunk.get("advanced"),
                    "pendingBefore": chunk.get("pendingBefore"),
                    "timedOut": bool(chunk.get("timedOut")),
                }
                for index, chunk in run["chunks"].items()
                if "elapsedSec" in chunk
            ],
            key=lambda item: item["elapsedSec"],
            reverse=True,
        )[:10],
    }


def status_summary(rows: list[dict[str, str]]) -> dict[str, Any]:
    by_status: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in rows:
        by_status[row.get("ocrStatus", "")].append(row)
    duration_rows = [
        as_float(row.get("ocrDurationSec"))
        for row in rows
        if row.get("ocrStatus") == "completed" and as_float(row.get("ocrDurationSec")) > 0
    ]
    attempts = Counter(as_int(row.get("attemptCount")) for row in rows)
    stuck_candidates = [
        row
        for row in rows
        if row.get("ocrStatus") == "pending" and as_int(row.get("attemptCount")) >= 2
    ]
    return {
        "statusCounts": {status: len(items) for status, items in sorted(by_status.items())},
        "sizeByStatus": {status: size_summary(items) for status, items in sorted(by_status.items())},
        "extensionByStatus": {
            status: dict(Counter(row.get("extension", "").lower() for row in items).most_common(12))
            for status, items in sorted(by_status.items())
        },
        "attemptCounts": dict(sorted(attempts.items())),
        "completedRecordedDurationSec": {
            "count": len(duration_rows),
            "avg": round(statistics.mean(duration_rows), 2) if duration_rows else 0.0,
            "median": round(statistics.median(duration_rows), 2) if duration_rows else 0.0,
            "p90": round(percentile(duration_rows, 0.90), 2) if duration_rows else 0.0,
            "max": round(max(duration_rows), 2) if duration_rows else 0.0,
            "note": "Duration is recorded per submitted batch and repeated on each row, so wall-clock chunk throughput is more reliable.",
        },
        "stuckCandidateCount": len(stuck_candidates),
        "stuckCandidateSize": size_summary(stuck_candidates),
        "stuckCandidateExtensions": dict(
            Counter(row.get("extension", "").lower() for row in stuck_candidates).most_common(12)
        ),
        "largestPending": [
            {
                "assetId": row.get("assetId"),
                "sourceMB": round(as_int(row.get("sourceBytes")) / 1024 / 1024, 2),
                "attemptCount": as_int(row.get("attemptCount")),
                "extension": row.get("extension"),
                "path": row.get("resolvedRelativePath"),
            }
            for row in sorted(
                by_status.get("pending", []),
                key=lambda item: as_int(item.get("sourceBytes")),
                reverse=True,
            )[:20]
        ],
        "topAttemptPending": [
            {
                "assetId": row.get("assetId"),
                "attemptCount": as_int(row.get("attemptCount")),
                "sourceMB": round(as_int(row.get("sourceBytes")) / 1024 / 1024, 2),
                "extension": row.get("extension"),
                "lastError": row.get("lastError"),
                "path": row.get("resolvedRelativePath"),
            }
            for row in sorted(
                by_status.get("pending", []),
                key=lambda item: (as_int(item.get("attemptCount")), as_int(item.get("sourceBytes"))),
                reverse=True,
            )[:20]
        ],
    }


def write_markdown(path: Path, report: dict[str, Any]) -> None:
    current = report["current"]
    status = current["status"]
    runs = current["runs"]
    latest_run = runs[-1] if runs else {}
    lines = [
        "# MinerU OCR Performance Report",
        "",
        f"- Generated at: {report['generatedAt']}",
        f"- Status CSV: `{report['statusCsv']}`",
        f"- Progress log: `{report['progressLog']}`",
        "",
        "## Current Status",
        "",
    ]
    for status_name, count in status["statusCounts"].items():
        lines.append(f"- {status_name}: {count}")
    lines.extend([
        "",
        "## Throughput",
        "",
    ])
    if latest_run:
        avg = latest_run.get("avgWallSecPerAdvancedFile")
        avg_text = "n/a" if avg is None else f"{avg} sec/file"
        lines.extend([
            f"- Latest run start: {latest_run.get('startedAt')}",
            f"- Completed chunks: {latest_run.get('chunkCount')}",
            f"- Files advanced by chunk pending-delta: {latest_run.get('advancedFiles')}",
            f"- Elapsed wall time in completed chunks: {latest_run.get('elapsedSec')} sec",
            f"- Average wall-clock time per advanced file: {avg_text}",
            f"- Zero-progress chunks: {latest_run.get('zeroProgressChunkCount')}",
            f"- Zero-progress elapsed: {latest_run.get('zeroProgressElapsedSec')} sec",
            f"- Timeout chunks: {latest_run.get('timeoutChunkCount')}",
        ])
    lines.extend([
        "",
        "## File Size",
        "",
    ])
    for status_name, summary in status["sizeByStatus"].items():
        if summary.get("count", 0) == 0:
            continue
        lines.append(
            f"- {status_name}: count={summary['count']}, median={summary['medianKB']} KB, "
            f"p90={summary['p90KB']} KB, max={summary['maxMB']} MB"
        )
    lines.extend([
        "",
        "## Stuck Candidates",
        "",
        f"- Pending rows with attemptCount >= 2: {status['stuckCandidateCount']}",
    ])
    stuck_size = status.get("stuckCandidateSize", {})
    if stuck_size.get("count"):
        lines.append(
            f"- Stuck candidate size: median={stuck_size['medianKB']} KB, "
            f"p90={stuck_size['p90KB']} KB, max={stuck_size['maxMB']} MB"
        )
    lines.append(f"- Stuck candidate extensions: `{json.dumps(status['stuckCandidateExtensions'], ensure_ascii=False)}`")
    lines.extend([
        "",
        "## Slowest Chunks",
        "",
        "| chunk | elapsedSec | advanced | pendingBefore | timedOut |",
        "|---:|---:|---:|---:|:---:|",
    ])
    for item in latest_run.get("slowestChunks", []):
        lines.append(
            f"| {item['chunk']} | {item['elapsedSec']} | {item['advanced']} | "
            f"{item['pendingBefore']} | {item['timedOut']} |"
        )
    lines.extend([
        "",
        "## Largest Pending Files",
        "",
        "| sourceMB | attempts | ext | path |",
        "|---:|---:|---|---|",
    ])
    for item in status["largestPending"][:10]:
        lines.append(
            f"| {item['sourceMB']} | {item['attemptCount']} | {item['extension']} | {item['path']} |"
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Analyze OCR throughput and stuck files.")
    parser.add_argument("--status-csv", required=True)
    parser.add_argument("--progress-log", required=True)
    parser.add_argument("--out-json", default="")
    parser.add_argument("--out-md", default="")
    args = parser.parse_args()

    status_path = Path(args.status_csv)
    log_path = Path(args.progress_log)
    rows, _ = read_status(status_path)
    runs = [summarize_run(run) for run in parse_progress_log(log_path)]
    report = {
        "generatedAt": datetime.now().isoformat(timespec="seconds"),
        "statusCsv": str(status_path),
        "progressLog": str(log_path),
        "current": {
            "status": status_summary(rows),
            "runs": runs,
        },
    }
    if args.out_json:
        Path(args.out_json).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    if args.out_md:
        write_markdown(Path(args.out_md), report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
