const fs = require("fs");
const path = require("path");

const [
  ,
  ,
  targetsPath,
  resultsCsvPath,
  summaryJsonPath,
  concurrencyArg = "8",
  timeoutArg = "15000",
] = process.argv;

if (!targetsPath || !resultsCsvPath || !summaryJsonPath) {
  console.error(
    "Usage: node check_article_404_correlation.cjs <targets.json> <results.csv> <summary.json> [concurrency] [timeoutMs]",
  );
  process.exit(2);
}

const concurrency = Math.max(1, Number.parseInt(concurrencyArg, 10) || 8);
const timeoutMs = Math.max(1000, Number.parseInt(timeoutArg, 10) || 15000);

function csvEscape(value) {
  const text = value === null || value === undefined ? "" : String(value);
  return /[",\r\n]/.test(text) ? `"${text.replace(/"/g, '""')}"` : text;
}

function statusName(statusCode) {
  if (statusCode >= 200 && statusCode < 400) return "ok";
  if (statusCode === 404) return "article_404";
  return "http_error";
}

async function checkTarget(target) {
  const rawUrl = String(target.articleUrl || "").trim();
  if (!rawUrl) {
    return { ...target, statusName: "missing_url", httpStatusCode: "", finalUrl: "", error: "" };
  }
  if (!/^https?:\/\//i.test(rawUrl)) {
    return { ...target, statusName: "not_http_url", httpStatusCode: "", finalUrl: "", error: "" };
  }

  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const response = await fetch(rawUrl, {
      method: "GET",
      redirect: "follow",
      signal: controller.signal,
      headers: {
        "User-Agent": "web-ocr2md-link-check/1.0",
        Accept: "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
      },
    });
    try {
      await response.body?.cancel();
    } catch {
      // Best effort: status code is already available.
    }
    return {
      ...target,
      statusName: statusName(response.status),
      httpStatusCode: response.status,
      finalUrl: response.url || "",
      error: "",
    };
  } catch (error) {
    return {
      ...target,
      statusName: "request_error",
      httpStatusCode: "",
      finalUrl: "",
      error: error && error.message ? error.message : String(error),
    };
  } finally {
    clearTimeout(timer);
  }
}

function summarize(results, predicate) {
  const scoped = results.filter(predicate);
  const counts = {};
  for (const row of scoped) {
    counts[row.statusName] = (counts[row.statusName] || 0) + 1;
  }
  const article404 = counts.article_404 || 0;
  return {
    totalWebMd: scoped.length,
    article404,
    article404Percent: scoped.length ? Number(((article404 / scoped.length) * 100).toFixed(2)) : 0,
    ok: counts.ok || 0,
    httpError: counts.http_error || 0,
    requestError: counts.request_error || 0,
    missingUrl: counts.missing_url || 0,
    notHttpUrl: counts.not_http_url || 0,
    statusCounts: counts,
  };
}

async function main() {
  const targets = JSON.parse(fs.readFileSync(targetsPath, "utf8"));
  const results = new Array(targets.length);
  let nextIndex = 0;
  let completed = 0;

  async function worker() {
    while (true) {
      const index = nextIndex++;
      if (index >= targets.length) return;
      results[index] = await checkTarget(targets[index]);
      completed++;
      if (completed % 100 === 0 || completed === targets.length) {
        console.log(`checked ${completed} / ${targets.length}`);
      }
    }
  }

  await Promise.all(Array.from({ length: concurrency }, () => worker()));

  fs.mkdirSync(path.dirname(resultsCsvPath), { recursive: true });
  fs.mkdirSync(path.dirname(summaryJsonPath), { recursive: true });

  const headers = [
    "mdRelativePath",
    "department",
    "articleUrl",
    "initialMissing",
    "confirmedMissing",
    "statusName",
    "httpStatusCode",
    "finalUrl",
    "error",
  ];
  const csv = [
    headers.join(","),
    ...results.map((row) => headers.map((header) => csvEscape(row[header])).join(",")),
  ].join("\r\n");
  fs.writeFileSync(resultsCsvPath, `\ufeff${csv}\r\n`, "utf8");

  const summary = {
    generatedAt: new Date().toISOString(),
    targetsPath,
    resultsCsvPath,
    concurrency,
    timeoutMs,
    allTargets: summarize(results, () => true),
    initialMissingWebMd: summarize(results, (row) => row.initialMissing === true),
    confirmedMissingWebMd: summarize(results, (row) => row.confirmedMissing === true),
  };
  fs.writeFileSync(summaryJsonPath, `${JSON.stringify(summary, null, 2)}\n`, "utf8");
}

main().catch((error) => {
  console.error(error);
  process.exit(1);
});
