param(
    [string]$SourceMdDir = (Join-Path $PSScriptRoot "..\..\網站文章MD"),
    [string]$WwwRoot = (Join-Path $PSScriptRoot "..\..\www\www"),
    [string]$OutDir = (Join-Path $PSScriptRoot "..\output"),
    [string]$MineruCommand = "mineru",
    [string]$Backend = "pipeline",
    [string]$Language = "ch",
    [int]$LargeMarkdownChars = 120000,
    [int]$MaxAssets = 0,
    [switch]$CheckArticleUrl,
    [int]$ArticleUrlTimeoutSec = 10,
    [switch]$SkipMissingRecheck,
    [switch]$SkipOcr,
    [switch]$OnlyPendingAssets,
    [switch]$Force,
    [switch]$AppendOversized
)

$ErrorActionPreference = "Stop"

try {
    [Console]::OutputEncoding = [System.Text.UTF8Encoding]::new()
    $OutputEncoding = [System.Text.UTF8Encoding]::new()
} catch {
}

$ocrExtensions = @(
    ".pdf",
    ".png", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff", ".webp",
    ".docx", ".pptx", ".xlsx"
)

$assetExtensions = $ocrExtensions + @(
    ".rtf", ".txt", ".csv", ".odt", ".ods", ".odp"
)

$pageLinkExtensions = @(".php")

$externalSchemes = @("mailto:", "tel:", "javascript:", "data:")

function Ensure-Directory {
    param([string]$Path)
    if (-not (Test-Path -LiteralPath $Path)) {
        New-Item -ItemType Directory -Path $Path -Force | Out-Null
    }
}

function Normalize-RelPath {
    param([string]$Path)
    if ([string]::IsNullOrWhiteSpace($Path)) {
        return ""
    }
    $value = $Path.Trim()
    $value = $value -replace "\\", "/"
    $value = $value -replace "^[./]+", ""
    $value = $value -replace "^/+", ""
    $value = $value -replace "/+", "/"
    return $value
}

function Decode-UrlValue {
    param([string]$Value)
    if ([string]::IsNullOrWhiteSpace($Value)) {
        return ""
    }
    $clean = $Value.Trim()
    $clean = $clean -replace "&amp;", "&"
    try {
        return [System.Uri]::UnescapeDataString($clean)
    } catch {
        return $clean
    }
}

function Get-ArticleUrl {
    param([string]$Text)
    $match = [regex]::Match($Text, "(?m)^-\s*\*\*文章網址\*\*:[ \t]*([^\r\n]*)[ \t]*\r?$")
    if ($match.Success) {
        return $match.Groups[1].Value.Trim()
    }
    return ""
}

function Get-ArticleHttpStatusName {
    param([int]$StatusCode)
    if ($StatusCode -ge 200 -and $StatusCode -lt 400) {
        return "ok"
    }
    if ($StatusCode -eq 404) {
        return "article_404"
    }
    return "http_error"
}

function Invoke-ArticleUrlRequest {
    param(
        [string]$Url,
        [string]$Method,
        [int]$TimeoutSec
    )

    $params = @{
        Uri = $Url
        Method = $Method
        TimeoutSec = $TimeoutSec
        MaximumRedirection = 5
        ErrorAction = "Stop"
    }
    if ($PSVersionTable.PSVersion.Major -lt 6) {
        $params.UseBasicParsing = $true
    }

    try {
        $response = Invoke-WebRequest @params
        $statusCode = [int]$response.StatusCode
        return [pscustomobject]@{
            Status = Get-ArticleHttpStatusName $statusCode
            HttpStatusCode = $statusCode
            FinalUri = if ($null -ne $response.BaseResponse -and $null -ne $response.BaseResponse.ResponseUri) { $response.BaseResponse.ResponseUri.AbsoluteUri } else { "" }
            Error = ""
        }
    } catch {
        $response = $_.Exception.Response
        if ($null -ne $response) {
            $statusCode = 0
            $finalUri = ""
            try { $statusCode = [int]$response.StatusCode } catch { }
            try {
                if ($null -ne $response.ResponseUri) {
                    $finalUri = $response.ResponseUri.AbsoluteUri
                }
            } catch { }

            return [pscustomobject]@{
                Status = if ($statusCode -gt 0) { Get-ArticleHttpStatusName $statusCode } else { "request_error" }
                HttpStatusCode = if ($statusCode -gt 0) { $statusCode } else { "" }
                FinalUri = $finalUri
                Error = $_.Exception.Message
            }
        }

        return [pscustomobject]@{
            Status = "request_error"
            HttpStatusCode = ""
            FinalUri = ""
            Error = $_.Exception.Message
        }
    }
}

function Test-ArticleUrl {
    param(
        [string]$Url,
        [int]$TimeoutSec
    )

    if ([string]::IsNullOrWhiteSpace($Url)) {
        return [pscustomobject]@{
            Status = "missing_url"
            HttpStatusCode = ""
            FinalUri = ""
            Error = ""
        }
    }

    $trimmed = $Url.Trim()
    if ($trimmed -notmatch "(?i)^https?://") {
        return [pscustomobject]@{
            Status = "not_http_url"
            HttpStatusCode = ""
            FinalUri = ""
            Error = ""
        }
    }

    $head = Invoke-ArticleUrlRequest $trimmed "Head" $TimeoutSec
    if ($head.HttpStatusCode -eq 405) {
        return Invoke-ArticleUrlRequest $trimmed "Get" $TimeoutSec
    }
    return $head
}

function Get-DepartmentName {
    param([string]$FullName)
    $name = [System.IO.Path]::GetFileNameWithoutExtension($FullName)
    $idx = $name.IndexOf("_content_")
    if ($idx -gt 0) {
        return $name.Substring(0, $idx)
    }
    return "(unknown)"
}

function Get-SectionText {
    param([string]$Text)
    $match = [regex]::Match($Text, "(?ms)^##\s*二、關聯的檔案資訊\s*(.*?)(?:^---\s*$|^##\s+|\z)")
    if ($match.Success) {
        return $match.Groups[1].Value
    }
    return ""
}

function Parse-RelationLinks {
    param([string]$Section)
    $links = New-Object System.Collections.Generic.List[object]
    if ([string]::IsNullOrWhiteSpace($Section)) {
        return $links
    }

    $current = $null
    foreach ($line in ($Section -split "`r?`n")) {
        $match = [regex]::Match($line, "^\s*-\s*\*\*\s*(\d+)\.\s*\[([^\]]+)\]\*\*:\s*(.*?)\s*$")
        if ($match.Success) {
            if ($null -ne $current) {
                $links.Add([pscustomobject]@{
                    Index = $current.Index
                    Label = $current.Label
                    Url = $current.Url.Trim()
                })
            }
            $current = [pscustomobject]@{
                Index = [int]$match.Groups[1].Value
                Label = $match.Groups[2].Value.Trim()
                Url = $match.Groups[3].Value.Trim()
            }
            continue
        }

        if ($null -ne $current -and $line -match "^\s+\S") {
            $current.Url += $line.Trim()
        }
    }

    if ($null -ne $current) {
        $links.Add([pscustomobject]@{
            Index = $current.Index
            Label = $current.Label
            Url = $current.Url.Trim()
        })
    }
    return $links
}

function Get-LinkInfo {
    param([string]$Url)

    $raw = $Url.Trim()
    $raw = $raw.Trim("<", ">")
    $lower = $raw.ToLowerInvariant()
    foreach ($scheme in $externalSchemes) {
        if ($lower.StartsWith($scheme)) {
            return [pscustomobject]@{
                IsExternal = $true
                IsYzu = $false
                Reason = "external_scheme"
                Path = ""
                Host = ""
                DecodedUrl = Decode-UrlValue $raw
            }
        }
    }

    if ($lower.StartsWith("//")) {
        $raw = "https:$raw"
        $lower = $raw.ToLowerInvariant()
    }

    if ($lower -match "^https?://") {
        try {
            $uri = [System.Uri]$raw
            $uriHost = $uri.Host.ToLowerInvariant()
            $isYzu = $uriHost -eq "www.yzu.edu.tw" -or $uriHost -eq "yzu.edu.tw"
            if (-not $isYzu) {
                return [pscustomobject]@{
                    IsExternal = $true
                    IsYzu = $false
                    Reason = "external_host"
                    Path = ""
                    Host = $uriHost
                    DecodedUrl = Decode-UrlValue $raw
                }
            }
            return [pscustomobject]@{
                IsExternal = $false
                IsYzu = $true
                Reason = "yzu_host"
                Path = Normalize-RelPath (Decode-UrlValue $uri.AbsolutePath)
                Host = $uriHost
                DecodedUrl = Decode-UrlValue $raw
            }
        } catch {
            return [pscustomobject]@{
                IsExternal = $true
                IsYzu = $false
                Reason = "bad_url"
                Path = ""
                Host = ""
                DecodedUrl = Decode-UrlValue $raw
            }
        }
    }

    $pathOnly = ($raw -split "[?#]", 2)[0]
    return [pscustomobject]@{
        IsExternal = $false
        IsYzu = $false
        Reason = "relative"
        Path = Normalize-RelPath (Decode-UrlValue $pathOnly)
        Host = ""
        DecodedUrl = Decode-UrlValue $raw
    }
}

function Get-ArticleBase {
    param(
        [string]$ArticleUrl,
        [System.Collections.Generic.HashSet[string]]$TopPrefixes
    )

    if ([string]::IsNullOrWhiteSpace($ArticleUrl)) {
        return [pscustomobject]@{ Base = ""; Prefix = ""; Confidence = "none" }
    }

    try {
        $uri = [System.Uri]$ArticleUrl
        $uriHost = $uri.Host.ToLowerInvariant()
        if ($uriHost -ne "www.yzu.edu.tw" -and $uriHost -ne "yzu.edu.tw") {
            return [pscustomobject]@{ Base = ""; Prefix = ""; Confidence = "external_article_url" }
        }

        $path = Normalize-RelPath (Decode-UrlValue $uri.AbsolutePath)
        $parts = @($path -split "/" | Where-Object { -not [string]::IsNullOrWhiteSpace($_) })
        if ($parts.Count -eq 0) {
            return [pscustomobject]@{ Base = ""; Prefix = ""; Confidence = "no_path" }
        }

        $prefix = $parts[0]
        $idx = [Array]::IndexOf($parts, "index.php")
        if ($idx -gt 0) {
            $base = ($parts[0..($idx - 1)] -join "/")
            return [pscustomobject]@{ Base = $base; Prefix = $prefix; Confidence = "article_url_index_php" }
        }

        if ($TopPrefixes.Contains($prefix.ToLowerInvariant())) {
            return [pscustomobject]@{ Base = $prefix; Prefix = $prefix; Confidence = "article_url_prefix_only" }
        }

        return [pscustomobject]@{ Base = ""; Prefix = ""; Confidence = "unknown_article_prefix" }
    } catch {
        return [pscustomobject]@{ Base = ""; Prefix = ""; Confidence = "bad_article_url" }
    }
}

function Get-ExtensionFromPath {
    param([string]$Path)
    if ([string]::IsNullOrWhiteSpace($Path)) {
        return ""
    }
    $clean = ($Path -split "[?#]", 2)[0]
    return [System.IO.Path]::GetExtension($clean).ToLowerInvariant()
}

function Test-AssetCandidate {
    param(
        [string]$Label,
        [string]$Path,
        [string]$Extension
    )

    $normalizedPath = Normalize-RelPath $Path
    $lowerPath = $normalizedPath.ToLowerInvariant()
    if ($pageLinkExtensions -contains $Extension) {
        return $false
    }
    if ($Label -match "圖片|檔案|附件") {
        return $true
    }
    if ($assetExtensions -contains $Extension) {
        return $true
    }
    if ($lowerPath -match "^(file|files|image|images)/") {
        return $true
    }
    return $false
}

function Get-ResolutionConfidence {
    param(
        [string]$Status,
        [string]$Method
    )

    if ($Status -eq "resolved") {
        if ($Method -eq "direct_or_article_base" -or $Method -eq "missing_recheck:candidate_path") {
            return "high"
        }
        if ($Method -eq "unique_suffix" -or $Method -match "unique_trailing_[34]_segments") {
            return "medium"
        }
        if ($Method -match "unique_filename_in_preferred_root" -or $Method -match "unique_trailing_2_segments") {
            return "low"
        }
        return "unknown"
    }

    if ($Status -eq "confirmed_missing") {
        return "high_for_missing"
    }
    if ($Status -eq "not_file_or_malformed") {
        return "high_for_exclusion"
    }
    if ($Status -eq "ambiguous") {
        return "needs_review"
    }
    return ""
}

function New-StableId {
    param([string]$Value)
    $sha1 = [System.Security.Cryptography.SHA1]::Create()
    try {
        $bytes = [System.Text.Encoding]::UTF8.GetBytes($Value)
        $hash = $sha1.ComputeHash($bytes)
        return (($hash | ForEach-Object { $_.ToString("x2") }) -join "").Substring(0, 16)
    } finally {
        $sha1.Dispose()
    }
}

function Write-JsonFile {
    param(
        [object]$Value,
        [string]$Path
    )
    $json = $Value | ConvertTo-Json -Depth 12
    Set-Content -LiteralPath $Path -Value $json -Encoding UTF8
}

function Escape-MdInline {
    param([string]$Value)
    if ($null -eq $Value) {
        return ""
    }
    return $Value.Replace("`r", " ").Replace("`n", " ")
}

function Get-ValidationStatus {
    param(
        [string]$Status,
        [int]$Chars,
        [long]$SourceBytes,
        [bool]$IsLarge
    )

    $reasons = New-Object System.Collections.Generic.List[string]
    if ($Status -ne "success") {
        $reasons.Add("ocr_not_successful")
    }
    if ($Status -eq "success" -and $Chars -eq 0) {
        $reasons.Add("empty_markdown")
    }
    if ($Status -eq "success" -and $Chars -gt 0 -and $Chars -lt 80 -and $SourceBytes -gt 50000) {
        $reasons.Add("very_short_for_source_size")
    }
    if ($IsLarge) {
        $reasons.Add("oversized_for_llm_context")
    }
    if ($reasons.Count -eq 0) {
        return "ok"
    }
    return ($reasons -join ";")
}

function Get-SafeAssetBaseName {
    param([string]$Path)
    $safeBase = [System.IO.Path]::GetFileNameWithoutExtension($Path)
    $safeBase = ($safeBase -replace '[^\p{L}\p{Nd}\._-]+', '_')
    if ($safeBase.Length -gt 80) {
        $safeBase = $safeBase.Substring(0, 80)
    }
    return $safeBase
}

function Get-AssetOcrMarkdownPath {
    param(
        [object]$Asset,
        [string]$AssetMarkdownDir
    )
    $assetId = New-StableId $Asset.resolvedRelativePath
    $safeBase = Get-SafeAssetBaseName $Asset.resolvedPath
    return Join-Path $AssetMarkdownDir "$assetId`_$safeBase.md"
}

Ensure-Directory $OutDir
$manifestDir = Join-Path $OutDir "manifest"
$rawOcrDir = Join-Path $OutDir "mineru_raw"
$assetMdDir = Join-Path $OutDir "ocr_md_assets"
$mergedDir = Join-Path $OutDir "web_md_with_ocr"
Ensure-Directory $manifestDir
Ensure-Directory $rawOcrDir
Ensure-Directory $assetMdDir
Ensure-Directory $mergedDir

$sourceRootFull = [System.IO.Path]::GetFullPath($SourceMdDir)
$wwwRootFull = [System.IO.Path]::GetFullPath($WwwRoot)
if (-not (Test-Path -LiteralPath $sourceRootFull)) {
    throw "SourceMdDir not found: $sourceRootFull"
}
if (-not (Test-Path -LiteralPath $wwwRootFull)) {
    throw "WwwRoot not found: $wwwRootFull"
}

Write-Host "Indexing www assets: $wwwRootFull"
$assetIndex = @{}
$fileNameIndex = @{}
$topPrefixes = New-Object System.Collections.Generic.HashSet[string]
foreach ($dir in Get-ChildItem -LiteralPath $wwwRootFull -Directory -ErrorAction SilentlyContinue) {
    [void]$topPrefixes.Add($dir.Name.ToLowerInvariant())
}

$wwwFiles = Get-ChildItem -LiteralPath $wwwRootFull -Recurse -File -ErrorAction SilentlyContinue
foreach ($file in $wwwFiles) {
    $relative = $file.FullName.Substring($wwwRootFull.Length) -replace '^[\\/]+', ''
    $relative = Normalize-RelPath $relative
    $key = $relative.ToLowerInvariant()
    if (-not $assetIndex.ContainsKey($key)) {
        $assetIndex[$key] = New-Object System.Collections.Generic.List[string]
    }
    $assetIndex[$key].Add($file.FullName)

    $fileNameKey = [System.IO.Path]::GetFileName($relative).ToLowerInvariant()
    if (-not $fileNameIndex.ContainsKey($fileNameKey)) {
        $fileNameIndex[$fileNameKey] = New-Object System.Collections.Generic.List[string]
    }
    $fileNameIndex[$fileNameKey].Add($key)
}

function Resolve-AssetPath {
    param(
        [string]$LinkPath,
        [object]$ArticleBase,
        [hashtable]$AssetIndex,
        [hashtable]$FileNameIndex
    )

    $normalized = Normalize-RelPath $LinkPath
    $candidates = New-Object System.Collections.Generic.List[string]
    if (-not [string]::IsNullOrWhiteSpace($normalized)) {
        $candidates.Add($normalized)
    }
    if (-not [string]::IsNullOrWhiteSpace($ArticleBase.Base) -and -not [string]::IsNullOrWhiteSpace($normalized)) {
        $candidates.Add((Normalize-RelPath (Join-Path $ArticleBase.Base $normalized)))
    }

    $found = New-Object System.Collections.Generic.List[string]
    $seen = New-Object System.Collections.Generic.HashSet[string]
    foreach ($candidate in $candidates) {
        $key = (Normalize-RelPath $candidate).ToLowerInvariant()
        if ($AssetIndex.ContainsKey($key)) {
            foreach ($full in $AssetIndex[$key]) {
                if ($seen.Add($full)) {
                    $found.Add($full)
                }
            }
        }
    }

    if ($found.Count -eq 1) {
        return [pscustomobject]@{ Status = "resolved"; ResolvedPath = $found[0]; ResolveMethod = "direct_or_article_base"; CandidateCount = 1; Candidates = @($found[0]) }
    }
    if ($found.Count -gt 1) {
        return [pscustomobject]@{ Status = "ambiguous"; ResolvedPath = ""; ResolveMethod = "direct_or_article_base"; CandidateCount = $found.Count; Candidates = @($found) }
    }

    $fileName = [System.IO.Path]::GetFileName($normalized).ToLowerInvariant()
    if ([string]::IsNullOrWhiteSpace($fileName) -or -not $FileNameIndex.ContainsKey($fileName)) {
        return [pscustomobject]@{ Status = "missing"; ResolvedPath = ""; ResolveMethod = "not_found"; CandidateCount = 0; Candidates = @() }
    }

    $suffixFound = New-Object System.Collections.Generic.List[string]
    $suffixKey = $normalized.ToLowerInvariant()
    foreach ($key in $FileNameIndex[$fileName]) {
        if ($key -eq $suffixKey -or $key.EndsWith("/$suffixKey")) {
            foreach ($full in $AssetIndex[$key]) {
                $suffixFound.Add($full)
            }
        }
    }

    if ($suffixFound.Count -eq 1) {
        return [pscustomobject]@{ Status = "resolved"; ResolvedPath = $suffixFound[0]; ResolveMethod = "unique_suffix"; CandidateCount = 1; Candidates = @($suffixFound[0]) }
    }
    if ($suffixFound.Count -gt 1) {
        return [pscustomobject]@{ Status = "ambiguous"; ResolvedPath = ""; ResolveMethod = "suffix_multiple_matches"; CandidateCount = $suffixFound.Count; Candidates = @($suffixFound) }
    }

    return [pscustomobject]@{ Status = "missing"; ResolvedPath = ""; ResolveMethod = "not_found"; CandidateCount = 0; Candidates = @() }
}

Write-Host "Scanning Markdown files: $sourceRootFull"
$records = New-Object System.Collections.Generic.List[object]
$articleRows = New-Object System.Collections.Generic.List[object]
$articleUrlCache = @{}
$mdStats = [ordered]@{
    sourceMdFiles = 0
    mdWithRelationSection = 0
    mdWithNonEmptyRelationSection = 0
    relationLinks = 0
    externalLinksSkipped = 0
    localAssetLinks = 0
    localAssetResolved = 0
    localAssetMissing = 0
    localAssetAmbiguous = 0
    localAssetUnsupportedForOcr = 0
}
$prefixStats = @{}

$mdFiles = Get-ChildItem -LiteralPath $sourceRootFull -Recurse -Filter *.md -File -ErrorAction SilentlyContinue
foreach ($md in $mdFiles) {
    $mdStats.sourceMdFiles++
    try {
        $text = Get-Content -LiteralPath $md.FullName -Raw -Encoding UTF8
    } catch {
        $records.Add([pscustomobject]@{
            recordType = "md_read_error"
            mdPath = $md.FullName
            error = $_.Exception.Message
        })
        continue
    }

    $section = Get-SectionText $text
    if (-not [string]::IsNullOrWhiteSpace($section)) {
        $mdStats.mdWithRelationSection++
    }
    if (-not [string]::IsNullOrWhiteSpace($section) -and $section -notmatch "（無關聯檔案）") {
        $mdStats.mdWithNonEmptyRelationSection++
    }

    $articleUrl = Get-ArticleUrl $text
    $articleBase = Get-ArticleBase $articleUrl $topPrefixes
    $dept = Get-DepartmentName $md.FullName
    $mdRel = Normalize-RelPath ($md.FullName.Substring($sourceRootFull.Length) -replace '^[\\/]+', '')
    $articleCheck = [pscustomobject]@{
        Status = "not_checked"
        HttpStatusCode = ""
        FinalUri = ""
        Error = ""
    }
    if ($CheckArticleUrl) {
        $cacheKey = $articleUrl.Trim().ToLowerInvariant()
        if (-not $articleUrlCache.ContainsKey($cacheKey)) {
            $articleUrlCache[$cacheKey] = Test-ArticleUrl $articleUrl $ArticleUrlTimeoutSec
        }
        $articleCheck = $articleUrlCache[$cacheKey]
    }
    $articleRows.Add([pscustomobject]@{
        mdPath = $md.FullName
        mdRelativePath = $mdRel
        department = $dept
        articleUrl = $articleUrl
        articlePrefix = $articleBase.Prefix
        articleBase = $articleBase.Base
        articleBaseConfidence = $articleBase.Confidence
        articleUrlCheckStatus = $articleCheck.Status
        articleHttpStatusCode = $articleCheck.HttpStatusCode
        articleUrlFinalUri = $articleCheck.FinalUri
        articleUrlError = $articleCheck.Error
    })
    if (-not $prefixStats.ContainsKey($dept)) {
        $prefixStats[$dept] = @{
            department = $dept
            articlePrefixes = @{}
            resolvedAssetPrefixes = @{}
            unknownCount = 0
            sampleArticleUrl = $articleUrl
        }
    }
    if (-not [string]::IsNullOrWhiteSpace($articleBase.Prefix)) {
        if (-not $prefixStats[$dept].articlePrefixes.ContainsKey($articleBase.Prefix)) {
            $prefixStats[$dept].articlePrefixes[$articleBase.Prefix] = 0
        }
        $prefixStats[$dept].articlePrefixes[$articleBase.Prefix]++
    }

    $links = Parse-RelationLinks $section
    foreach ($link in $links) {
        $mdStats.relationLinks++
        $info = Get-LinkInfo $link.Url
        if ($info.IsExternal) {
            $mdStats.externalLinksSkipped++
            continue
        }

        $ext = Get-ExtensionFromPath $info.Path
        $isAsset = Test-AssetCandidate $link.Label $info.Path $ext
        if (-not $isAsset) {
            continue
        }

        $mdStats.localAssetLinks++
        $resolve = Resolve-AssetPath $info.Path $articleBase $assetIndex $fileNameIndex
        if ($resolve.Status -eq "resolved") {
            $mdStats.localAssetResolved++
        } elseif ($resolve.Status -eq "ambiguous") {
            $mdStats.localAssetAmbiguous++
        } else {
            $mdStats.localAssetMissing++
        }

        $supported = $ocrExtensions -contains $ext
        if (-not $supported) {
            $mdStats.localAssetUnsupportedForOcr++
        }

        $resolvedRel = ""
        $resolvedPrefix = ""
        $sourceBytes = 0
        if ($resolve.Status -eq "resolved") {
            $resolvedRel = Normalize-RelPath ($resolve.ResolvedPath.Substring($wwwRootFull.Length) -replace '^[\\/]+', '')
            $parts = $resolvedRel -split "/"
            if ($parts.Count -gt 0) {
                $resolvedPrefix = $parts[0]
                if (-not $prefixStats[$dept].resolvedAssetPrefixes.ContainsKey($resolvedPrefix)) {
                    $prefixStats[$dept].resolvedAssetPrefixes[$resolvedPrefix] = 0
                }
                $prefixStats[$dept].resolvedAssetPrefixes[$resolvedPrefix]++
            }
            $sourceBytes = (Get-Item -LiteralPath $resolve.ResolvedPath).Length
        } elseif ([string]::IsNullOrWhiteSpace($articleBase.Prefix)) {
            $prefixStats[$dept].unknownCount++
        }

        $stableValue = "$mdRel|$($link.Index)|$($info.DecodedUrl)|$resolvedRel"
        $records.Add([pscustomobject]@{
            id = New-StableId $stableValue
            mdPath = $md.FullName
            mdRelativePath = $mdRel
            department = $dept
            articleUrl = $articleUrl
            articlePrefix = $articleBase.Prefix
            articleBase = $articleBase.Base
            articleBaseConfidence = $articleBase.Confidence
            articleUrlCheckStatus = $articleCheck.Status
            articleHttpStatusCode = $articleCheck.HttpStatusCode
            articleUrlFinalUri = $articleCheck.FinalUri
            articleUrlError = $articleCheck.Error
            relationIndex = $link.Index
            relationLabel = $link.Label
            relationUrl = $link.Url
            decodedUrl = $info.DecodedUrl
            linkPath = $info.Path
            extension = $ext
            isOcrSupportedExtension = $supported
            initialResolveStatus = $resolve.Status
            initialResolveMethod = $resolve.ResolveMethod
            resolveStatus = $resolve.Status
            resolveMethod = $resolve.ResolveMethod
            resolutionStage = if ($resolve.Status -eq "resolved") { "initial_scan" } else { "initial_scan_unresolved" }
            resolutionConfidence = Get-ResolutionConfidence $resolve.Status $resolve.ResolveMethod
            missingRecheckStatus = ""
            missingRecheckMethod = ""
            missingRecheckResolvedRelativePath = ""
            missingRecheckCandidatePaths = ""
            candidateCount = $resolve.CandidateCount
            candidatePaths = ($resolve.Candidates -join " | ")
            resolvedPath = $resolve.ResolvedPath
            resolvedRelativePath = $resolvedRel
            resolvedPrefix = $resolvedPrefix
            sourceBytes = $sourceBytes
            ocrStatus = if ($SkipOcr) { "skipped_by_flag" } elseif (-not $supported) { "unsupported_extension" } else { "pending" }
            ocrMarkdownPath = ""
            ocrChars = 0
            isLargeForLlm = $false
            validationStatus = if (-not $supported) { "unsupported_extension" } else { "pending" }
            error = ""
        })
    }
}

$manifestCsv = Join-Path $manifestDir "related_assets_manifest.csv"
$initialManifestCsv = Join-Path $manifestDir "related_assets_manifest.initial.csv"
$records | Export-Csv -LiteralPath $initialManifestCsv -NoTypeInformation -Encoding UTF8BOM

$articleStatusCsv = Join-Path $manifestDir "article_url_status.csv"
$articleRows | Export-Csv -LiteralPath $articleStatusCsv -NoTypeInformation -Encoding UTF8BOM

$missingCsv = Join-Path $manifestDir "missing_assets.csv"
$missingRecheckDir = Join-Path $manifestDir "missing_recheck"
$missingRecheckReportCsv = Join-Path $missingRecheckDir "missing_recheck_report.csv"
$missingRecheckSummaryJson = Join-Path $missingRecheckDir "missing_recheck_summary.json"
$missingRecheckStats = [ordered]@{
    enabled = -not [bool]$SkipMissingRecheck
    missingRowsInput = 0
    recheckResolved = 0
    recheckAmbiguous = 0
    recheckNotFileOrMalformed = 0
    recheckConfirmedMissing = 0
    uniqueLinkRows = 0
    uniqueResolved = 0
    uniqueAmbiguous = 0
    uniqueNotFileOrMalformed = 0
    uniqueConfirmedMissing = 0
    error = ""
}

$missingRecords = @($records | Where-Object { $_.resolveStatus -eq "missing" })
$missingRecheckStats.missingRowsInput = $missingRecords.Count
$missingRecords | Export-Csv -LiteralPath $missingCsv -NoTypeInformation -Encoding UTF8BOM

if (-not $SkipMissingRecheck -and $missingRecords.Count -gt 0) {
    $missingRecheckScript = Join-Path $PSScriptRoot "Resolve-MissingAssets.ps1"
    if (Test-Path -LiteralPath $missingRecheckScript) {
        Write-Host "Rechecking missing asset rows: $($missingRecords.Count)"
        & $missingRecheckScript -MissingCsv $missingCsv -WwwRoot $wwwRootFull -OutDir $missingRecheckDir

        if (Test-Path -LiteralPath $missingRecheckSummaryJson) {
            try {
                $summaryText = Get-Content -LiteralPath $missingRecheckSummaryJson -Raw -Encoding UTF8
                $recheckSummary = $summaryText | ConvertFrom-Json
                $missingRecheckStats.recheckResolved = [int]$recheckSummary.recheckResolved
                $missingRecheckStats.recheckAmbiguous = [int]$recheckSummary.recheckAmbiguous
                $missingRecheckStats.recheckNotFileOrMalformed = [int]$recheckSummary.recheckNotFileOrMalformed
                $missingRecheckStats.recheckConfirmedMissing = [int]$recheckSummary.recheckConfirmedMissing
                $missingRecheckStats.uniqueLinkRows = [int]$recheckSummary.uniqueLinkRows
                $missingRecheckStats.uniqueResolved = [int]$recheckSummary.uniqueResolved
                $missingRecheckStats.uniqueAmbiguous = [int]$recheckSummary.uniqueAmbiguous
                $missingRecheckStats.uniqueNotFileOrMalformed = [int]$recheckSummary.uniqueNotFileOrMalformed
                $missingRecheckStats.uniqueConfirmedMissing = [int]$recheckSummary.uniqueConfirmedMissing
            } catch {
                $missingRecheckStats.error = "Failed to read missing recheck summary: $($_.Exception.Message)"
            }
        }

        if (Test-Path -LiteralPath $missingRecheckReportCsv) {
            $recordById = @{}
            foreach ($record in $records) {
                if (-not [string]::IsNullOrWhiteSpace($record.id) -and -not $recordById.ContainsKey($record.id)) {
                    $recordById[$record.id] = $record
                }
            }

            foreach ($row in @(Import-Csv -LiteralPath $missingRecheckReportCsv)) {
                if ([string]::IsNullOrWhiteSpace($row.id) -or -not $recordById.ContainsKey($row.id)) {
                    continue
                }

                $record = $recordById[$row.id]
                $record.missingRecheckStatus = $row.recheckStatus
                $record.missingRecheckMethod = $row.recheckMethod
                $record.missingRecheckResolvedRelativePath = $row.resolvedRelativePath
                $record.missingRecheckCandidatePaths = $row.candidateRelativePaths

                if ($row.recheckStatus -eq "resolved") {
                    $resolvedFullPath = $row.resolvedFullPath
                    if ([string]::IsNullOrWhiteSpace($resolvedFullPath) -and -not [string]::IsNullOrWhiteSpace($row.resolvedRelativePath)) {
                        $resolvedFullPath = Join-Path $wwwRootFull ($row.resolvedRelativePath -replace "/", [System.IO.Path]::DirectorySeparatorChar)
                    }

                    $record.resolveStatus = "resolved"
                    $record.resolveMethod = "missing_recheck:$($row.recheckMethod)"
                    $record.resolutionStage = "missing_recheck"
                    $record.candidateCount = [int]$row.candidateCount
                    $record.candidatePaths = $row.candidateRelativePaths
                    $record.resolvedPath = $resolvedFullPath
                    $record.resolvedRelativePath = Normalize-RelPath $row.resolvedRelativePath
                    $resolvedParts = @($record.resolvedRelativePath -split "/" | Where-Object { -not [string]::IsNullOrWhiteSpace($_) })
                    if ($resolvedParts.Count -gt 0) {
                        $record.resolvedPrefix = $resolvedParts[0]
                        if (-not $prefixStats[$record.department].resolvedAssetPrefixes.ContainsKey($record.resolvedPrefix)) {
                            $prefixStats[$record.department].resolvedAssetPrefixes[$record.resolvedPrefix] = 0
                        }
                        $prefixStats[$record.department].resolvedAssetPrefixes[$record.resolvedPrefix]++
                    }
                    if (-not [string]::IsNullOrWhiteSpace($resolvedFullPath) -and (Test-Path -LiteralPath $resolvedFullPath)) {
                        $record.sourceBytes = (Get-Item -LiteralPath $resolvedFullPath).Length
                    }
                } elseif ($row.recheckStatus -eq "ambiguous") {
                    $record.resolveStatus = "ambiguous"
                    $record.resolveMethod = "missing_recheck:$($row.recheckMethod)"
                    $record.resolutionStage = "missing_recheck"
                    $record.candidateCount = [int]$row.candidateCount
                    $record.candidatePaths = $row.candidateRelativePaths
                } elseif ($row.recheckStatus -eq "not_file_or_malformed") {
                    $record.resolveStatus = "not_file_or_malformed"
                    $record.resolveMethod = "missing_recheck:$($row.recheckMethod)"
                    $record.resolutionStage = "missing_recheck"
                } elseif ($row.recheckStatus -eq "confirmed_missing") {
                    $record.resolveStatus = "confirmed_missing"
                    $record.resolveMethod = "missing_recheck:$($row.recheckMethod)"
                    $record.resolutionStage = "missing_recheck"
                }
                $record.resolutionConfidence = Get-ResolutionConfidence $record.resolveStatus $record.resolveMethod
            }
        }
    } else {
        $missingRecheckStats.error = "Missing recheck script not found: $missingRecheckScript"
    }
}

$prefixRows = New-Object System.Collections.Generic.List[object]
foreach ($entry in $prefixStats.GetEnumerator()) {
    $articlePrefixPairs = @()
    foreach ($prefix in $entry.Value.articlePrefixes.Keys) {
        $articlePrefixPairs += "$prefix=$($entry.Value.articlePrefixes[$prefix])"
    }
    $assetPrefixPairs = @()
    foreach ($prefix in $entry.Value.resolvedAssetPrefixes.Keys) {
        $assetPrefixPairs += "$prefix=$($entry.Value.resolvedAssetPrefixes[$prefix])"
    }

    $allPrefixes = New-Object System.Collections.Generic.HashSet[string]
    foreach ($prefix in $entry.Value.articlePrefixes.Keys) { [void]$allPrefixes.Add($prefix) }
    foreach ($prefix in $entry.Value.resolvedAssetPrefixes.Keys) { [void]$allPrefixes.Add($prefix) }

    $status = "unknown"
    if ($allPrefixes.Count -eq 1) {
        $status = "resolved"
    } elseif ($allPrefixes.Count -gt 1) {
        $status = "ambiguous"
    }

    $prefixRows.Add([pscustomobject]@{
        department = $entry.Value.department
        status = $status
        prefixes = (($allPrefixes | Sort-Object) -join " | ")
        articlePrefixCounts = ($articlePrefixPairs -join " | ")
        resolvedAssetPrefixCounts = ($assetPrefixPairs -join " | ")
        unknownCount = $entry.Value.unknownCount
        sampleArticleUrl = $entry.Value.sampleArticleUrl
    })
}
$prefixCsv = Join-Path $manifestDir "department_prefix_mapping.csv"
$prefixRows | Sort-Object department | Export-Csv -LiteralPath $prefixCsv -NoTypeInformation -Encoding UTF8BOM

$uniqueAssets = $records |
    Where-Object { $_.resolveStatus -eq "resolved" -and $_.isOcrSupportedExtension } |
    Group-Object resolvedPath |
    ForEach-Object { $_.Group[0] }

if ($OnlyPendingAssets -and -not $Force) {
    $uniqueAssets = $uniqueAssets | Where-Object {
        $assetMdPath = Get-AssetOcrMarkdownPath $_ $assetMdDir
        -not (Test-Path -LiteralPath $assetMdPath)
    }
}

if ($MaxAssets -gt 0) {
    $uniqueAssets = $uniqueAssets | Select-Object -First $MaxAssets
}

$mineruAvailable = $false
if (-not $SkipOcr) {
    $mineruAvailable = $null -ne (Get-Command $MineruCommand -ErrorAction SilentlyContinue)
    if (-not $mineruAvailable) {
        foreach ($record in $records) {
            if ($record.ocrStatus -eq "pending") {
                $record.ocrStatus = "error"
                $record.validationStatus = "ocr_not_successful"
                $record.error = "MinerU command not found: $MineruCommand"
            }
        }
    }
}

if (-not $SkipOcr -and $mineruAvailable) {
    Write-Host "Running MinerU OCR for $($uniqueAssets.Count) unique asset(s)"
    foreach ($asset in $uniqueAssets) {
        $assetId = New-StableId $asset.resolvedRelativePath
        $assetOutDir = Join-Path $rawOcrDir $assetId
        $assetMdPath = Get-AssetOcrMarkdownPath $asset $assetMdDir
        Ensure-Directory $assetOutDir

        $status = "success"
        $errorMessage = ""
        if ((-not $Force) -and (Test-Path -LiteralPath $assetMdPath)) {
            $status = "success"
        } else {
            $logPath = Join-Path $assetOutDir "mineru.log"
            $errPath = Join-Path $assetOutDir "mineru.err.log"
            try {
                & $MineruCommand -p $asset.resolvedPath -o $assetOutDir -b $Backend -l $Language 1> $logPath 2> $errPath
                if ($LASTEXITCODE -ne 0) {
                    $status = "error"
                    $errorMessage = "MinerU exited with code $LASTEXITCODE"
                }
            } catch {
                $status = "error"
                $errorMessage = $_.Exception.Message
            }

            if ($status -eq "success") {
                $generated = Get-ChildItem -LiteralPath $assetOutDir -Recurse -Filter *.md -File -ErrorAction SilentlyContinue |
                    Sort-Object Length -Descending |
                    Select-Object -First 1
                if ($null -eq $generated) {
                    $status = "error"
                    $errorMessage = "MinerU did not produce a Markdown file"
                } else {
                    Copy-Item -LiteralPath $generated.FullName -Destination $assetMdPath -Force
                }
            }
        }

        $chars = 0
        $isLarge = $false
        if ($status -eq "success" -and (Test-Path -LiteralPath $assetMdPath)) {
            $content = Get-Content -LiteralPath $assetMdPath -Raw -Encoding UTF8
            $chars = $content.Length
            $isLarge = $chars -gt $LargeMarkdownChars
        }

        foreach ($record in ($records | Where-Object { $_.resolvedPath -eq $asset.resolvedPath })) {
            $record.ocrStatus = $status
            $record.ocrMarkdownPath = if ($status -eq "success") { $assetMdPath } else { "" }
            $record.ocrChars = $chars
            $record.isLargeForLlm = $isLarge
            $record.validationStatus = Get-ValidationStatus $status $chars $record.sourceBytes $isLarge
            $record.error = $errorMessage
        }
    }
}

Write-Host "Writing merged Markdown files"
$recordsByMd = $records |
    Where-Object { $_.ocrStatus -eq "success" -and -not [string]::IsNullOrWhiteSpace($_.ocrMarkdownPath) } |
    Group-Object mdPath

$mergedStats = [ordered]@{
    mdWithSuccessfulOcr = 0
    mdWrittenWithOcr = 0
    ocrAssetsAppended = 0
    oversizedAssetsMarked = 0
    oversizedAssetsNotAppended = 0
}

foreach ($group in $recordsByMd) {
    $sourceMd = $group.Name
    $sourceText = Get-Content -LiteralPath $sourceMd -Raw -Encoding UTF8
    $rel = Normalize-RelPath ($sourceMd.Substring($sourceRootFull.Length) -replace '^[\\/]+', '')
    $targetPath = Join-Path $mergedDir $rel
    $sourceFullPath = [System.IO.Path]::GetFullPath($sourceMd)
    $targetFullPath = [System.IO.Path]::GetFullPath($targetPath)
    if ([StringComparer]::OrdinalIgnoreCase.Equals($sourceFullPath, $targetFullPath)) {
        throw "Refusing to write OCR merge output over the source Markdown: $sourceFullPath"
    }
    Ensure-Directory ([System.IO.Path]::GetDirectoryName($targetPath))

    $append = New-Object System.Text.StringBuilder
    [void]$append.AppendLine("")
    [void]$append.AppendLine("---")
    [void]$append.AppendLine("")
    [void]$append.AppendLine("## 三、關聯檔案 OCR Markdown")
    [void]$append.AppendLine("")
    [void]$append.AppendLine("> 以下內容由 MinerU 從關聯圖片或檔案產生。標記為 oversized_for_llm_context 的項目不建議直接整段放進 LLM 上下文。")
    [void]$append.AppendLine("")

    $mergedStats.mdWithSuccessfulOcr++
    $seenAssetMd = New-Object System.Collections.Generic.HashSet[string]
    foreach ($record in ($group.Group | Sort-Object relationIndex)) {
        if (-not $seenAssetMd.Add($record.ocrMarkdownPath)) {
            continue
        }

        [void]$append.AppendLine("### OCR $($record.relationIndex): $(Escape-MdInline $record.relationUrl)")
        [void]$append.AppendLine("")
        [void]$append.AppendLine("- 關聯類型: $($record.relationLabel)")
        [void]$append.AppendLine("- 本機檔案: $($record.resolvedRelativePath)")
        [void]$append.AppendLine("- OCR Markdown: $($record.ocrMarkdownPath)")
        [void]$append.AppendLine("- OCR 字數: $($record.ocrChars)")
        [void]$append.AppendLine("- 驗證狀態: $($record.validationStatus)")
        [void]$append.AppendLine("")

        if ($record.isLargeForLlm) {
            $mergedStats.oversizedAssetsMarked++
            if (-not $AppendOversized) {
                $mergedStats.oversizedAssetsNotAppended++
                [void]$append.AppendLine("> 此 OCR Markdown 超過 LargeMarkdownChars=$LargeMarkdownChars，已標記但未內嵌全文；請改讀上方 OCR Markdown 檔案。")
                [void]$append.AppendLine("")
                continue
            }
        }

        [void]$append.AppendLine('```markdown')
        $ocrText = Get-Content -LiteralPath $record.ocrMarkdownPath -Raw -Encoding UTF8
        [void]$append.AppendLine($ocrText.TrimEnd())
        [void]$append.AppendLine('```')
        [void]$append.AppendLine("")
        $mergedStats.ocrAssetsAppended++
    }

    $baseText = $sourceText
    if (Test-Path -LiteralPath $targetPath) {
        $existingTargetText = Get-Content -LiteralPath $targetPath -Raw -Encoding UTF8
        $baseText = $existingTargetText -replace '(?ms)\r?\n---\r?\n\r?\n## 三、關聯檔案 OCR Markdown\r?\n.*\z', ''
    }

    Set-Content -LiteralPath $targetPath -Value ($baseText.TrimEnd() + $append.ToString()) -Encoding UTF8
    $mergedStats.mdWrittenWithOcr++
}

$uniqueAssetsCount = ($uniqueAssets | Measure-Object).Count
$ocrSuccessCount = ($records | Where-Object { $_.ocrStatus -eq "success" } | Measure-Object).Count
$ocrErrorCount = ($records | Where-Object { $_.ocrStatus -eq "error" } | Measure-Object).Count
$ocrSkippedCount = ($records | Where-Object { $_.ocrStatus -like "skipped*" -or $_.ocrStatus -eq "unsupported_extension" } | Measure-Object).Count
$oversizedCount = ($records | Where-Object { $_.isLargeForLlm } | Measure-Object).Count

$ocrStats = [ordered]@{
    uniqueAssetsSelectedForOcr = $uniqueAssetsCount
    ocrSuccessRecords = $ocrSuccessCount
    ocrErrorRecords = $ocrErrorCount
    ocrSkippedRecords = $ocrSkippedCount
    oversizedRecords = $oversizedCount
}

$articleUrlStats = [ordered]@{
    checkArticleUrl = [bool]$CheckArticleUrl
    timeoutSec = $ArticleUrlTimeoutSec
    articleRows = ($articleRows | Measure-Object).Count
    uniqueArticleUrlsCached = $articleUrlCache.Count
    notChecked = ($articleRows | Where-Object { $_.articleUrlCheckStatus -eq "not_checked" } | Measure-Object).Count
    ok = ($articleRows | Where-Object { $_.articleUrlCheckStatus -eq "ok" } | Measure-Object).Count
    article404 = ($articleRows | Where-Object { $_.articleUrlCheckStatus -eq "article_404" } | Measure-Object).Count
    httpError = ($articleRows | Where-Object { $_.articleUrlCheckStatus -eq "http_error" } | Measure-Object).Count
    requestError = ($articleRows | Where-Object { $_.articleUrlCheckStatus -eq "request_error" } | Measure-Object).Count
    missingUrl = ($articleRows | Where-Object { $_.articleUrlCheckStatus -eq "missing_url" } | Measure-Object).Count
    notHttpUrl = ($articleRows | Where-Object { $_.articleUrlCheckStatus -eq "not_http_url" } | Measure-Object).Count
}

$finalAssetStats = [ordered]@{
    assetRecords = ($records | Measure-Object).Count
    resolved = ($records | Where-Object { $_.resolveStatus -eq "resolved" } | Measure-Object).Count
    resolvedByInitialScan = ($records | Where-Object { $_.resolveStatus -eq "resolved" -and $_.resolutionStage -eq "initial_scan" } | Measure-Object).Count
    resolvedByMissingRecheck = ($records | Where-Object { $_.resolveStatus -eq "resolved" -and $_.resolutionStage -eq "missing_recheck" } | Measure-Object).Count
    confirmedMissing = ($records | Where-Object { $_.resolveStatus -eq "confirmed_missing" } | Measure-Object).Count
    ambiguous = ($records | Where-Object { $_.resolveStatus -eq "ambiguous" } | Measure-Object).Count
    notFileOrMalformed = ($records | Where-Object { $_.resolveStatus -eq "not_file_or_malformed" } | Measure-Object).Count
    stillMissingWithoutRecheck = ($records | Where-Object { $_.resolveStatus -eq "missing" } | Measure-Object).Count
    unsupportedForOcr = ($records | Where-Object { $_.isOcrSupportedExtension -ne $true -and $_.isOcrSupportedExtension -ne "True" } | Measure-Object).Count
    resolvedOcrSupportedRecords = ($records | Where-Object { $_.resolveStatus -eq "resolved" -and ($_.isOcrSupportedExtension -eq $true -or $_.isOcrSupportedExtension -eq "True") } | Measure-Object).Count
    uniqueResolvedOcrSupportedAssets = ($records | Where-Object { $_.resolveStatus -eq "resolved" -and ($_.isOcrSupportedExtension -eq $true -or $_.isOcrSupportedExtension -eq "True") } | Select-Object -ExpandProperty resolvedRelativePath -Unique | Measure-Object).Count
    highConfidenceResolved = ($records | Where-Object { $_.resolveStatus -eq "resolved" -and $_.resolutionConfidence -eq "high" } | Measure-Object).Count
    mediumConfidenceResolved = ($records | Where-Object { $_.resolveStatus -eq "resolved" -and $_.resolutionConfidence -eq "medium" } | Measure-Object).Count
    lowConfidenceResolved = ($records | Where-Object { $_.resolveStatus -eq "resolved" -and $_.resolutionConfidence -eq "low" } | Measure-Object).Count
}

$records | Export-Csv -LiteralPath $manifestCsv -NoTypeInformation -Encoding UTF8BOM
$articleRows | Export-Csv -LiteralPath $articleStatusCsv -NoTypeInformation -Encoding UTF8BOM

$oversizedCsv = Join-Path $manifestDir "oversized_assets.csv"
$records | Where-Object { $_.isLargeForLlm } | Export-Csv -LiteralPath $oversizedCsv -NoTypeInformation -Encoding UTF8BOM

$errorCsv = Join-Path $manifestDir "errors_and_unresolved.csv"
$records | Where-Object {
    $_.resolveStatus -ne "resolved" -or $_.ocrStatus -eq "error" -or $_.validationStatus -ne "ok"
} | Export-Csv -LiteralPath $errorCsv -NoTypeInformation -Encoding UTF8BOM

$summary = [ordered]@{
    generatedAt = (Get-Date).ToString("s")
    sourceMdDir = $sourceRootFull
    wwwRoot = $wwwRootFull
    outputDir = [System.IO.Path]::GetFullPath($OutDir)
    skipOcr = [bool]$SkipOcr
    mineruCommand = $MineruCommand
    mineruAvailable = $mineruAvailable
    backend = $Backend
    language = $Language
    largeMarkdownChars = $LargeMarkdownChars
    markdownStats = $mdStats
    finalAssetStats = $finalAssetStats
    missingRecheckStats = $missingRecheckStats
    articleUrlStats = $articleUrlStats
    ocrStats = $ocrStats
    mergedStats = $mergedStats
    files = [ordered]@{
        manifestCsv = $manifestCsv
        initialManifestCsv = $initialManifestCsv
        missingCsv = $missingCsv
        missingRecheckReportCsv = $missingRecheckReportCsv
        missingRecheckSummaryJson = $missingRecheckSummaryJson
        articleUrlStatusCsv = $articleStatusCsv
        prefixMappingCsv = $prefixCsv
        oversizedCsv = $oversizedCsv
        errorsAndUnresolvedCsv = $errorCsv
        mergedMarkdownDir = $mergedDir
        ocrMarkdownAssetDir = $assetMdDir
    }
}

$summaryPath = Join-Path $OutDir "summary.json"
Write-JsonFile $summary $summaryPath

Write-Host "Done."
Write-Host "Summary: $summaryPath"
Write-Host "Manifest: $manifestCsv"
Write-Host "Article URL status: $articleStatusCsv"
Write-Host "Prefix mapping: $prefixCsv"
Write-Host "Merged Markdown dir: $mergedDir"
