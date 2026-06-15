param(
    [Parameter(Mandatory = $true)]
    [string]$JobsCsv,

    [Parameter(Mandatory = $true)]
    [string]$OutDir,

    [int]$ChunkLimit = 500,
    [int]$MaxChunks = 200,
    [int]$ChunkTimeoutSec = 7200,
    [int]$MaxStaleRunningAttempts = 1,
    [bool]$UsePersistentApi = $true,
    [int]$ApiPort = 0,
    [int]$ApiStartupTimeoutSec = 600,
    [int]$ApiMaxConcurrentRequests = 3
)

$ErrorActionPreference = 'Stop'
$root = Resolve-Path (Join-Path $PSScriptRoot '..\..')
$python = Join-Path $root 'mineru_ocr_workspace\.venv\Scripts\python.exe'
$runner = Join-Path $root 'mineru_ocr_workspace\scripts\Invoke-OcrJobsApi.py'
$progressLog = Join-Path $OutDir 'ocr_gpu_remaining_v5_chunk_progress.log'
$runStamp = Get-Date -Format 'yyyyMMdd_HHmmss'
$apiProcess = $null
$apiUrl = $null
$apiRestartCount = 0

function Write-ProgressLog {
    param([string]$Message)
    $line = "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') $Message"
    Add-Content -Path $progressLog -Value $line -Encoding UTF8
    Write-Output $line
}

function Get-StatusCounts {
    $rows = Import-Csv $JobsCsv
    $counts = @{}
    foreach ($group in ($rows | Group-Object ocrStatus)) {
        $counts[$group.Name] = $group.Count
    }
    return $counts
}

function Get-FreeTcpPort {
    $listener = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Parse('127.0.0.1'), 0)
    try {
        $listener.Start()
        return [int]$listener.LocalEndpoint.Port
    } finally {
        $listener.Stop()
    }
}

function Wait-ApiHealthy {
    param(
        [string]$BaseUrl,
        [object]$Process,
        [int]$TimeoutSec
    )

    $started = Get-Date
    while (((Get-Date) - $started).TotalSeconds -lt $TimeoutSec) {
        if ($Process -ne $null) {
            try {
                $Process.Refresh()
                if ($Process.HasExited) {
                    throw "MinerU API process exited before becoming healthy; exitCode=$($Process.ExitCode)"
                }
            } catch {
                throw
            }
        }
        try {
            $health = Invoke-RestMethod -Uri "$BaseUrl/health" -Method Get -TimeoutSec 5
            if ($health.status -eq 'healthy') {
                return $true
            }
        } catch {
            Start-Sleep -Seconds 2
        }
    }
    throw "Timed out waiting for MinerU API health: ${BaseUrl}"
}

function Test-ApiHealthy {
    param(
        [string]$BaseUrl,
        [object]$Process
    )

    if ($Process -eq $null -or [string]::IsNullOrWhiteSpace($BaseUrl)) {
        return $false
    }
    try {
        $Process.Refresh()
        if ($Process.HasExited) {
            return $false
        }
        $health = Invoke-RestMethod -Uri "$BaseUrl/health" -Method Get -TimeoutSec 5
        return ($health.status -eq 'healthy')
    } catch {
        return $false
    }
}

function Stop-ProcessTree {
    param([int]$ProcessId)
    try {
        $children = @(Get-CimInstance Win32_Process | Where-Object { $_.ParentProcessId -eq $ProcessId })
        foreach ($child in $children) {
            Stop-ProcessTree -ProcessId ([int]$child.ProcessId)
        }
    } catch {
        Write-ProgressLog "warning: failed to enumerate process children for pid=${ProcessId}: $($_.Exception.Message)"
    }
    Stop-Process -Id $ProcessId -Force -ErrorAction SilentlyContinue
}

function Stop-OcrApiServer {
    param([object]$Process)
    if ($Process -eq $null) {
        return
    }
    try {
        $Process.Refresh()
        if (-not $Process.HasExited) {
            Write-ProgressLog "stopping persistent MinerU API pid=$($Process.Id)"
            Stop-ProcessTree -ProcessId ([int]$Process.Id)
            Start-Sleep -Seconds 5
        }
    } catch {
        Write-ProgressLog "warning: failed to stop persistent MinerU API pid=$($Process.Id): $($_.Exception.Message)"
    }
}

function Start-OcrApiServer {
    param([int]$RestartIndex)

    $resolvedPort = if ($ApiPort -gt 0) { $ApiPort } else { Get-FreeTcpPort }
    $baseUrl = "http://127.0.0.1:$resolvedPort"
    $apiOut = Join-Path $OutDir ("mineru_api_output_{0}_{1:000}" -f $runStamp, $RestartIndex)
    $apiStdout = Join-Path $OutDir ("mineru_api_{0}_{1:000}_stdout.log" -f $runStamp, $RestartIndex)
    $apiStderr = Join-Path $OutDir ("mineru_api_{0}_{1:000}_stderr.log" -f $runStamp, $RestartIndex)
    New-Item -ItemType Directory -Path $apiOut -Force | Out-Null

    $oldOutputRoot = $env:MINERU_API_OUTPUT_ROOT
    $oldMaxConcurrent = $env:MINERU_API_MAX_CONCURRENT_REQUESTS
    $oldDisableAccessLog = $env:MINERU_API_DISABLE_ACCESS_LOG
    $oldShutdownOnStdin = $env:MINERU_API_SHUTDOWN_ON_STDIN_EOF
    try {
        $env:MINERU_API_OUTPUT_ROOT = $apiOut
        $env:MINERU_API_MAX_CONCURRENT_REQUESTS = [string]$ApiMaxConcurrentRequests
        $env:MINERU_API_DISABLE_ACCESS_LOG = '1'
        Remove-Item Env:\MINERU_API_SHUTDOWN_ON_STDIN_EOF -ErrorAction SilentlyContinue
        $apiArgs = @('-m', 'mineru.cli.fast_api', '--host', '127.0.0.1', '--port', [string]$resolvedPort)
        Write-ProgressLog "starting persistent MinerU API restart=$RestartIndex url=$baseUrl"
        $process = Start-Process -FilePath $python -ArgumentList $apiArgs -WorkingDirectory $root -WindowStyle Hidden -RedirectStandardOutput $apiStdout -RedirectStandardError $apiStderr -PassThru
    } finally {
        if ($null -eq $oldOutputRoot) { Remove-Item Env:\MINERU_API_OUTPUT_ROOT -ErrorAction SilentlyContinue } else { $env:MINERU_API_OUTPUT_ROOT = $oldOutputRoot }
        if ($null -eq $oldMaxConcurrent) { Remove-Item Env:\MINERU_API_MAX_CONCURRENT_REQUESTS -ErrorAction SilentlyContinue } else { $env:MINERU_API_MAX_CONCURRENT_REQUESTS = $oldMaxConcurrent }
        if ($null -eq $oldDisableAccessLog) { Remove-Item Env:\MINERU_API_DISABLE_ACCESS_LOG -ErrorAction SilentlyContinue } else { $env:MINERU_API_DISABLE_ACCESS_LOG = $oldDisableAccessLog }
        if ($null -eq $oldShutdownOnStdin) { Remove-Item Env:\MINERU_API_SHUTDOWN_ON_STDIN_EOF -ErrorAction SilentlyContinue } else { $env:MINERU_API_SHUTDOWN_ON_STDIN_EOF = $oldShutdownOnStdin }
    }

    Wait-ApiHealthy -BaseUrl $baseUrl -Process $process -TimeoutSec $ApiStartupTimeoutSec | Out-Null
    Write-ProgressLog "persistent MinerU API healthy restart=$RestartIndex pid=$($process.Id) url=$baseUrl stdout=$apiStdout stderr=$apiStderr"
    return [pscustomobject]@{
        Process = $process
        Url = $baseUrl
    }
}

function Reset-RetriableErrors {
    $rows = Import-Csv $JobsCsv
    $changed = 0
    foreach ($row in $rows) {
        if ($row.ocrStatus -ne 'error') {
            continue
        }
        $err = [string]$row.lastError
        if (
            $err -like '*WinError 10061*' -or
            $err -like '*拒絕連線*' -or
            $err -like '*Connection refused*' -or
            $err -like '*WinError 5*ocr_asset_status.csv.tmp*'
        ) {
            $row.ocrStatus = 'pending'
            $row.lastError = "Reset retriable infrastructure error after chunk: $err"
            $changed += 1
        }
    }
    if ($changed -gt 0) {
        $tmp = "$JobsCsv.reset.tmp"
        $rows | Export-Csv -Path $tmp -NoTypeInformation -Encoding UTF8
        Move-Item -LiteralPath $tmp -Destination $JobsCsv -Force
    }
    return $changed
}

function Reset-StaleRunning {
    $rows = Import-Csv $JobsCsv
    $changed = 0
    $timedOut = 0
    foreach ($row in $rows) {
        if ($row.ocrStatus -eq 'running') {
            $attempt = 0
            [void][int]::TryParse([string]$row.attemptCount, [ref]$attempt)
            if ($attempt -ge $MaxStaleRunningAttempts) {
                $row.ocrStatus = 'timeout'
                $row.lastError = "Deferred stale running row after chunk timeout; attemptCount=$attempt"
                $timedOut += 1
            } else {
                $row.ocrStatus = 'pending'
                $row.lastError = 'Reset stale running status before next chunk'
            }
            $changed += 1
        }
    }
    if ($changed -gt 0) {
        $tmp = "$JobsCsv.running.tmp"
        $rows | Export-Csv -Path $tmp -NoTypeInformation -Encoding UTF8
        Move-Item -LiteralPath $tmp -Destination $JobsCsv -Force
    }
    return [pscustomobject]@{
        Reset = $changed
        TimedOut = $timedOut
    }
}

Write-ProgressLog "chunked OCR start; chunkLimit=$ChunkLimit maxChunks=$MaxChunks jobsCsv=$JobsCsv usePersistentApi=$UsePersistentApi"

try {
if ($UsePersistentApi) {
    $startedApi = Start-OcrApiServer -RestartIndex $apiRestartCount
    $apiProcess = $startedApi.Process
    $apiUrl = $startedApi.Url
}

for ($chunk = 1; $chunk -le $MaxChunks; $chunk++) {
    $resetRunning = Reset-StaleRunning
    $reset = Reset-RetriableErrors
    $counts = Get-StatusCounts
    $pending = if ($counts.ContainsKey('pending')) { [int]$counts['pending'] } else { 0 }
    $running = if ($counts.ContainsKey('running')) { [int]$counts['running'] } else { 0 }
    Write-ProgressLog "before chunk=$chunk pending=$pending running=$running resetRunning=$($resetRunning.Reset) resetRetriable=$reset resetStaleTimeout=$($resetRunning.TimedOut)"

    if ($pending -le 0 -and $running -le 0) {
        Write-ProgressLog "no pending/running rows remain; stopping"
        break
    }

    if ($UsePersistentApi -and -not (Test-ApiHealthy -BaseUrl $apiUrl -Process $apiProcess)) {
        Write-ProgressLog "persistent MinerU API is not healthy before chunk=$chunk; restarting"
        Stop-OcrApiServer -Process $apiProcess
        $apiRestartCount += 1
        $startedApi = Start-OcrApiServer -RestartIndex $apiRestartCount
        $apiProcess = $startedApi.Process
        $apiUrl = $startedApi.Url
    }

    $stdout = Join-Path $OutDir ("chunk_{0}_{1:000}_stdout.log" -f $runStamp, $chunk)
    $stderr = Join-Path $OutDir ("chunk_{0}_{1:000}_stderr.log" -f $runStamp, $chunk)
    $args = @(
        $runner,
        '--jobs-csv', $JobsCsv,
        '--out-dir', $OutDir,
        '--backend', 'pipeline',
        '--language', 'chinese_cht',
        '--limit', "$ChunkLimit",
        '--timeout-sec', '900',
        '--concurrency', '3',
        '--batch-size', '16',
        '--retries', '0',
        '--retry-delay-sec', '2',
        '--skip-error-retry',
        '--skip-timeout-retry'
    )
    if ($UsePersistentApi) {
        $args += @('--api-url', $apiUrl)
    }

    $started = Get-Date
    $process = Start-Process -FilePath $python -ArgumentList $args -WorkingDirectory $root -WindowStyle Hidden -RedirectStandardOutput $stdout -RedirectStandardError $stderr -PassThru
    $sawRunning = $false
    $chunkTimedOut = $false
    while (-not $process.HasExited) {
        Start-Sleep -Seconds 15
        $elapsedNow = ((Get-Date) - $started).TotalSeconds
        $loopCounts = Get-StatusCounts
        $loopRunning = if ($loopCounts.ContainsKey('running')) { [int]$loopCounts['running'] } else { 0 }
        if ($loopRunning -gt 0) {
            $sawRunning = $true
        }
        if ($sawRunning -and $loopRunning -eq 0) {
            Write-ProgressLog "chunk=$chunk has no running rows but process pid=$($process.Id) is still alive; waiting for runner cleanup"
            if ($process.WaitForExit(30000)) {
                break
            }
            Write-ProgressLog "chunk=$chunk runner cleanup exceeded 30s; stopping stale process pid=$($process.Id)"
            Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue
            Start-Sleep -Seconds 5
            break
        }
        if ($elapsedNow -gt $ChunkTimeoutSec) {
            Write-ProgressLog "chunk=$chunk exceeded timeoutSec=$ChunkTimeoutSec; stopping process pid=$($process.Id)"
            $chunkTimedOut = $true
            Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue
            Start-Sleep -Seconds 5
            break
        }
        try {
            $process.Refresh()
        } catch {
            break
        }
    }
    $elapsed = [math]::Round(((Get-Date) - $started).TotalSeconds, 2)
    $exitCode = if ($process.HasExited) { $process.ExitCode } else { 'timeout_stopped' }
    Write-ProgressLog "after chunk=$chunk exit=$exitCode elapsedSec=$elapsed stdout=$stdout stderr=$stderr"

    $chunkFailed = ([string]$exitCode -ne '0')
    if ($UsePersistentApi -and ($chunkTimedOut -or $chunkFailed -or -not (Test-ApiHealthy -BaseUrl $apiUrl -Process $apiProcess))) {
        Write-ProgressLog "chunk=$chunk ended with timeout=$chunkTimedOut exit=$exitCode or unhealthy API; restarting persistent MinerU API before next chunk"
        Stop-OcrApiServer -Process $apiProcess
        $apiRestartCount += 1
        $startedApi = Start-OcrApiServer -RestartIndex $apiRestartCount
        $apiProcess = $startedApi.Process
        $apiUrl = $startedApi.Url
    }
}

$finalCounts = Get-StatusCounts
Write-ProgressLog ("final counts: " + (($finalCounts.GetEnumerator() | Sort-Object Name | ForEach-Object { "$($_.Key)=$($_.Value)" }) -join ' '))
} finally {
    if ($UsePersistentApi) {
        Stop-OcrApiServer -Process $apiProcess
    }
}
