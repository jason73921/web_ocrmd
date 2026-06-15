param(
    [string]$WorkspaceDir = (Join-Path $PSScriptRoot ".."),
    [string]$Python = "python"
)

$ErrorActionPreference = "Stop"
$venvDir = Join-Path $WorkspaceDir ".venv"
$uvCacheDir = Join-Path $WorkspaceDir ".uv-cache"

if (-not (Test-Path -LiteralPath $uvCacheDir)) {
    New-Item -ItemType Directory -Path $uvCacheDir -Force | Out-Null
}
$env:UV_CACHE_DIR = $uvCacheDir

if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    throw "uv is required. Install uv first, then rerun this script."
}

if (-not (Test-Path -LiteralPath $venvDir)) {
    & uv venv $venvDir --python $Python
    if ($LASTEXITCODE -ne 0) {
        throw "uv venv failed with exit code $LASTEXITCODE"
    }
}

$pythonExe = Join-Path $venvDir "Scripts\python.exe"
& uv pip install --python $pythonExe -U "mineru[all]"
if ($LASTEXITCODE -ne 0) {
    throw "uv pip install failed with exit code $LASTEXITCODE"
}

Write-Host "MinerU setup complete."
Write-Host "Activate with: $venvDir\Scripts\Activate.ps1"
Write-Host "Verify with: mineru --help"
