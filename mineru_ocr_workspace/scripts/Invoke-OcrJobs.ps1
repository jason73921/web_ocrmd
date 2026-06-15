param(
    [string]$JobsCsv,
    [string]$OutDir,
    [string]$MineruCommand = (Join-Path $PSScriptRoot "..\.venv\Scripts\mineru.exe"),
    [string]$Backend = "pipeline",
    [string]$Language = "chinese_cht",
    [int]$Limit = 100,
    [int]$TimeoutSec = 900,
    [switch]$Force
)

$ErrorActionPreference = "Stop"

try {
    [Console]::OutputEncoding = [System.Text.UTF8Encoding]::new()
    $OutputEncoding = [System.Text.UTF8Encoding]::new()
} catch {
}

function Ensure-Directory {
    param([string]$Path)
    if (-not (Test-Path -LiteralPath $Path)) {
        New-Item -ItemType Directory -Path $Path -Force | Out-Null
    }
}

function Export-CsvUtf8Bom {
    param(
        [object[]]$Rows,
        [string]$Path
    )
    $Rows | Export-Csv -LiteralPath $Path -NoTypeInformation -Encoding UTF8BOM
}

function Get-SafeAssetBaseName {
    param([string]$Path)
    $safeBase = [System.IO.Path]::GetFileNameWithoutExtension($Path)
    $safeBase = ($safeBase -replace '[^\p{L}\p{Nd}\._-]+', '_')
    if ($safeBase.Length -gt 80) {
        $safeBase = $safeBase.Substring(0, 80)
    }
    if ([string]::IsNullOrWhiteSpace($safeBase)) {
        return "asset"
    }
    return $safeBase
}

if ([string]::IsNullOrWhiteSpace($JobsCsv)) {
    throw "JobsCsv is required."
}
if ([string]::IsNullOrWhiteSpace($OutDir)) {
    throw "OutDir is required."
}

$jobsPath = [System.IO.Path]::GetFullPath($JobsCsv)
$outDirFull = [System.IO.Path]::GetFullPath($OutDir)
$assetMdDir = Join-Path $outDirFull "ocr_md_assets"
$rawDir = Join-Path $outDirFull "mineru_raw"
$statusCsv = Join-Path $outDirFull "ocr_asset_status.csv"
$runSummaryPath = Join-Path $outDirFull "ocr_run_summary.json"
Ensure-Directory $outDirFull
Ensure-Directory $assetMdDir
Ensure-Directory $rawDir

if (-not (Test-Path -LiteralPath $jobsPath)) {
    throw "JobsCsv not found: $jobsPath"
}
if (-not (Test-Path -LiteralPath $MineruCommand)) {
    throw "MineruCommand not found: $MineruCommand"
}

$jobs = @(Import-Csv -LiteralPath $jobsPath)
$processed = 0
$skippedCached = 0
$success = 0
$errors = 0
$timeouts = 0
$startedAt = Get-Date

foreach ($job in $jobs) {
    if ($processed -ge $Limit) {
        break
    }

    $alreadyDone = $false
    if (-not $Force) {
        if ($job.ocrStatus -like "completed*" -and -not [string]::IsNullOrWhiteSpace($job.ocrMarkdownPath) -and (Test-Path -LiteralPath $job.ocrMarkdownPath)) {
            $alreadyDone = $true
        }
    }
    if ($alreadyDone) {
        $skippedCached++
        continue
    }

    if ($job.ocrStatus -notin @("pending", "error", "timeout") -and -not $Force) {
        continue
    }

    $processed++
    $assetId = $job.assetId
    $sourcePath = $job.resolvedPath
    $assetOutDir = Join-Path $rawDir $assetId
    Ensure-Directory $assetOutDir
    $safeBase = Get-SafeAssetBaseName $sourcePath
    $assetMdPath = Join-Path $assetMdDir "$assetId`_$safeBase.md"
    $logPath = Join-Path $assetOutDir "mineru.log"
    $errPath = Join-Path $assetOutDir "mineru.err.log"

    $job.attemptCount = ([int]($job.attemptCount -as [int]) + 1).ToString()
    $job.ocrStatus = "running"
    $job.lastError = ""
    Export-CsvUtf8Bom $jobs $statusCsv

    Write-Host "[$processed/$Limit] OCR $($job.extension) $($job.sourceBytes) bytes: $($job.resolvedRelativePath)"

    $sw = [System.Diagnostics.Stopwatch]::StartNew()
    try {
        if (-not (Test-Path -LiteralPath $sourcePath)) {
            throw "Source file not found: $sourcePath"
        }

        $proc = Start-Process -FilePath $MineruCommand `
            -ArgumentList @("-p", $sourcePath, "-o", $assetOutDir, "-b", $Backend, "-l", $Language) `
            -NoNewWindow -PassThru `
            -RedirectStandardOutput $logPath `
            -RedirectStandardError $errPath

        if (-not $proc.WaitForExit($TimeoutSec * 1000)) {
            try { $proc.Kill($true) } catch { try { $proc.Kill() } catch { } }
            $job.ocrStatus = "timeout"
            $job.lastError = "MinerU timed out after $TimeoutSec seconds"
            $timeouts++
        } elseif ($proc.ExitCode -ne 0) {
            $job.ocrStatus = "error"
            $job.lastError = "MinerU exited with code $($proc.ExitCode)"
            $errors++
        } else {
            $generated = Get-ChildItem -LiteralPath $assetOutDir -Recurse -Filter *.md -File -ErrorAction SilentlyContinue |
                Sort-Object Length -Descending |
                Select-Object -First 1
            if ($null -eq $generated) {
                $job.ocrStatus = "error"
                $job.lastError = "MinerU did not produce a Markdown file"
                $errors++
            } else {
                Copy-Item -LiteralPath $generated.FullName -Destination $assetMdPath -Force
                $content = Get-Content -LiteralPath $assetMdPath -Raw -Encoding UTF8
                $job.ocrStatus = "completed"
                $job.ocrMarkdownPath = $assetMdPath
                $job.ocrGeneratedAt = (Get-Date).ToString("s")
                $job.ocrChars = $content.Length.ToString()
                $job.needsReocr = "N"
                $success++
            }
        }
    } catch {
        $job.ocrStatus = "error"
        $job.lastError = $_.Exception.Message
        $errors++
    } finally {
        $sw.Stop()
        $job.ocrDurationSec = [Math]::Round($sw.Elapsed.TotalSeconds, 2).ToString()
        Export-CsvUtf8Bom $jobs $statusCsv
    }
}

$summary = [ordered]@{
    generatedAt = (Get-Date).ToString("s")
    jobsCsv = $jobsPath
    outDir = $outDirFull
    mineruCommand = $MineruCommand
    backend = $Backend
    language = $Language
    limit = $Limit
    timeoutSec = $TimeoutSec
    processedThisRun = $processed
    skippedCachedThisRun = $skippedCached
    successThisRun = $success
    errorsThisRun = $errors
    timeoutsThisRun = $timeouts
    totalCompleted = @($jobs | Where-Object { $_.ocrStatus -like "completed*" }).Count
    totalPending = @($jobs | Where-Object { $_.ocrStatus -eq "pending" }).Count
    totalError = @($jobs | Where-Object { $_.ocrStatus -eq "error" }).Count
    totalTimeout = @($jobs | Where-Object { $_.ocrStatus -eq "timeout" }).Count
    elapsedSec = [Math]::Round(((Get-Date) - $startedAt).TotalSeconds, 2)
    outputs = [ordered]@{
        statusCsv = $statusCsv
        ocrMarkdownDir = $assetMdDir
        rawDir = $rawDir
    }
}
$summary | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $runSummaryPath -Encoding UTF8
Write-Host "Done."
Write-Host "Status: $statusCsv"
Write-Host "Summary: $runSummaryPath"
