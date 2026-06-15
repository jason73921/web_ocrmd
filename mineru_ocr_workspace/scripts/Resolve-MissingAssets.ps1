param(
    [string]$MissingCsv = (Join-Path $PSScriptRoot "..\output\manifest\missing_assets.csv"),
    [string]$WwwRoot = (Join-Path $PSScriptRoot "..\..\www\www"),
    [string]$OutDir = (Join-Path $PSScriptRoot "..\output\manifest\missing_recheck")
)

$ErrorActionPreference = "Stop"

try {
    [Console]::OutputEncoding = [System.Text.UTF8Encoding]::new()
    $OutputEncoding = [System.Text.UTF8Encoding]::new()
} catch {
}

$knownFileExtensions = New-Object System.Collections.Generic.HashSet[string]
@(
    ".pdf", ".png", ".jpg", ".jpeg", ".bmp", ".gif", ".tif", ".tiff", ".webp",
    ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
    ".odt", ".ods", ".odp", ".rtf", ".txt", ".csv",
    ".zip", ".rar", ".7z"
) | ForEach-Object { [void]$knownFileExtensions.Add($_) }

$pageLinkExtensions = New-Object System.Collections.Generic.HashSet[string]
@(".php") | ForEach-Object { [void]$pageLinkExtensions.Add($_) }

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
    $value = [System.Net.WebUtility]::HtmlDecode($Path.Trim())
    $value = $value.Trim("<", ">", '"', "'")
    $value = ($value -split "[?#]", 2)[0]
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
    $clean = [System.Net.WebUtility]::HtmlDecode($Value.Trim())
    try {
        return [System.Uri]::UnescapeDataString($clean)
    } catch {
        return $clean
    }
}

function Get-PathKey {
    param([string]$Path)
    $normalized = Normalize-RelPath $Path
    if ([string]::IsNullOrWhiteSpace($normalized)) {
        return ""
    }
    return $normalized.Normalize([System.Text.NormalizationForm]::FormKC).ToLowerInvariant()
}

function Get-RelativeFromFullPath {
    param(
        [string]$FullPath,
        [string]$Root
    )
    return (Normalize-RelPath ($FullPath.Substring($Root.Length) -replace '^[\\/]+', ''))
}

function Add-HashList {
    param(
        [hashtable]$Hash,
        [string]$Key,
        [string]$Value
    )
    if ([string]::IsNullOrWhiteSpace($Key)) {
        return
    }
    if (-not $Hash.ContainsKey($Key)) {
        $Hash[$Key] = New-Object System.Collections.Generic.List[string]
    }
    $Hash[$Key].Add($Value)
}

function Add-Candidate {
    param(
        [System.Collections.Generic.List[string]]$Candidates,
        [System.Collections.Generic.HashSet[string]]$Seen,
        [string]$Path
    )
    $normalized = Normalize-RelPath $Path
    if ([string]::IsNullOrWhiteSpace($normalized)) {
        return
    }
    $key = Get-PathKey $normalized
    if ($Seen.Add($key)) {
        $Candidates.Add($normalized)
    }
}

function Get-ArticleBaseFromUrl {
    param(
        [string]$ArticleUrl,
        [System.Collections.Generic.HashSet[string]]$TopPrefixes
    )

    if ([string]::IsNullOrWhiteSpace($ArticleUrl)) {
        return ""
    }

    try {
        $decoded = Decode-UrlValue $ArticleUrl
        $uri = [System.Uri]$decoded
        $host = $uri.Host.ToLowerInvariant()
        if ($host -ne "www.yzu.edu.tw" -and $host -ne "yzu.edu.tw") {
            return ""
        }
        $path = Normalize-RelPath (Decode-UrlValue $uri.AbsolutePath)
        $parts = @($path -split "/" | Where-Object { -not [string]::IsNullOrWhiteSpace($_) })
        if ($parts.Count -eq 0) {
            return ""
        }
        $idx = [Array]::IndexOf($parts, "index.php")
        if ($idx -gt 0) {
            return ($parts[0..($idx - 1)] -join "/")
        }
        if ($TopPrefixes.Contains($parts[0].ToLowerInvariant())) {
            return $parts[0]
        }
    } catch {
        return ""
    }
    return ""
}

function Get-DepartmentRoot {
    param([string]$Department)

    switch ($Department) {
        "元智首頁" { return "home" }
        "認識元智" { return "aboutyzu" }
        "招生資訊" { return "admissions" }
        "校友資訊" { return "alumni" }
        "圖書館" { return "library" }
        default { return "" }
    }
}

function Test-KnownTopPrefix {
    param(
        [string]$Path,
        [System.Collections.Generic.HashSet[string]]$TopPrefixes
    )
    $normalized = Normalize-RelPath $Path
    if ([string]::IsNullOrWhiteSpace($normalized)) {
        return $false
    }
    $first = ($normalized -split "/", 2)[0].ToLowerInvariant()
    return $TopPrefixes.Contains($first)
}

function Get-LinkPathFromRelationUrl {
    param([string]$RelationUrl)

    if ([string]::IsNullOrWhiteSpace($RelationUrl)) {
        return ""
    }

    $decoded = Decode-UrlValue $RelationUrl
    $decoded = $decoded -replace '\\"', '"'
    $decoded = $decoded.Trim()
    $embeddedUrl = [regex]::Match($decoded, "(?i)https?:/{1,2}[^""'<>\\]+")
    if ($embeddedUrl.Success) {
        $decoded = $embeddedUrl.Value
    }
    $decoded = $decoded.Trim('"', "'", "\", " ")
    $decoded = $decoded -replace "^(https?):/([^/])", '$1://$2'
    $lower = $decoded.ToLowerInvariant()
    if ($lower.StartsWith("//")) {
        $decoded = "https:$decoded"
        $lower = $decoded.ToLowerInvariant()
    }

    $localYzuMatch = [regex]::Match($decoded, "(?i)^https?://(?:www\.)?yzu\.edu\.tw/?(?<path>[^?#]*)")
    if ($localYzuMatch.Success) {
        return Normalize-RelPath (Decode-UrlValue $localYzuMatch.Groups["path"].Value)
    }

    if ($lower -match "^https?://") {
        try {
            $uri = [System.Uri]$decoded
            $host = $uri.Host.ToLowerInvariant()
            if ($host -eq "www.yzu.edu.tw" -or $host -eq "yzu.edu.tw") {
                return Normalize-RelPath (Decode-UrlValue $uri.AbsolutePath)
            }
            return ""
        } catch {
            return ""
        }
    }

    return Normalize-RelPath $decoded
}

function Get-PreferredRoots {
    param(
        [object]$Row,
        [System.Collections.Generic.HashSet[string]]$TopPrefixes
    )

    $roots = New-Object System.Collections.Generic.List[string]
    $seen = New-Object System.Collections.Generic.HashSet[string]

    $articleBase = Normalize-RelPath $Row.articleBase
    if ([string]::IsNullOrWhiteSpace($articleBase)) {
        $articleBase = Get-ArticleBaseFromUrl $Row.articleUrl $TopPrefixes
    }
    if (-not [string]::IsNullOrWhiteSpace($articleBase) -and $seen.Add((Get-PathKey $articleBase))) {
        $roots.Add($articleBase)
    }

    $departmentRoot = Get-DepartmentRoot $Row.department
    if (-not [string]::IsNullOrWhiteSpace($departmentRoot) -and $seen.Add((Get-PathKey $departmentRoot))) {
        $roots.Add($departmentRoot)
    }

    return $roots
}

function Test-ProbablyFileLike {
    param(
        [object]$Row,
        [string]$Path
    )

    $relation = [string]$Row.relationUrl
    $cleanPath = Normalize-RelPath $Path
    $cleanRelation = [System.Net.WebUtility]::HtmlDecode($relation.Trim())
    $lower = $cleanRelation.ToLowerInvariant()

    if ([string]::IsNullOrWhiteSpace($cleanPath)) {
        return [pscustomobject]@{ IsFileLike = $false; Reason = "empty_path" }
    }
    if ($lower -match "^https?:&?$" -or $lower -match "^https?:amp;$") {
        return [pscustomobject]@{ IsFileLike = $false; Reason = "malformed_url_token" }
    }
    if ($lower.StartsWith("mailto:") -or $lower.StartsWith("tel:") -or $lower.StartsWith("javascript:") -or $lower.StartsWith("data:")) {
        return [pscustomobject]@{ IsFileLike = $false; Reason = "external_scheme" }
    }

    $extension = [System.IO.Path]::GetExtension($cleanPath).ToLowerInvariant()
    if ($pageLinkExtensions.Contains($extension)) {
        return [pscustomobject]@{ IsFileLike = $false; Reason = "page_link_extension" }
    }
    if ($knownFileExtensions.Contains($extension)) {
        return [pscustomobject]@{ IsFileLike = $true; Reason = "known_extension" }
    }

    $firstSegment = ($cleanPath -split "/", 2)[0].ToLowerInvariant()
    if ($firstSegment -match "^(file|files|image|images|template|templates|download|downloads|attachment|attachments|donation)$") {
        return [pscustomobject]@{ IsFileLike = $true; Reason = "asset_directory_without_known_extension" }
    }

    if ($cleanPath.Contains("/") -and -not [string]::IsNullOrWhiteSpace($extension) -and $extension.Length -le 12) {
        return [pscustomobject]@{ IsFileLike = $true; Reason = "path_with_extension" }
    }

    return [pscustomobject]@{ IsFileLike = $false; Reason = "plain_text_or_navigation_label" }
}

function Find-ByExactCandidates {
    param(
        [System.Collections.Generic.List[string]]$Candidates,
        [hashtable]$ExactIndex,
        [hashtable]$NormIndex,
        [string]$Method
    )

    $matches = New-Object System.Collections.Generic.List[string]
    $seen = New-Object System.Collections.Generic.HashSet[string]
    foreach ($candidate in $Candidates) {
        $exactKey = (Normalize-RelPath $candidate).ToLowerInvariant()
        if ($ExactIndex.ContainsKey($exactKey)) {
            foreach ($full in $ExactIndex[$exactKey]) {
                if ($seen.Add($full)) {
                    $matches.Add($full)
                }
            }
        }
    }
    if ($matches.Count -eq 1) {
        return [pscustomobject]@{ Status = "resolved"; Method = $Method; Matches = @($matches[0]) }
    }
    if ($matches.Count -gt 1) {
        return [pscustomobject]@{ Status = "ambiguous"; Method = $Method; Matches = @($matches) }
    }

    foreach ($candidate in $Candidates) {
        $normKey = Get-PathKey $candidate
        if ($NormIndex.ContainsKey($normKey)) {
            foreach ($full in $NormIndex[$normKey]) {
                if ($seen.Add($full)) {
                    $matches.Add($full)
                }
            }
        }
    }
    if ($matches.Count -eq 1) {
        return [pscustomobject]@{ Status = "resolved"; Method = "$Method`_unicode_normalized"; Matches = @($matches[0]) }
    }
    if ($matches.Count -gt 1) {
        return [pscustomobject]@{ Status = "ambiguous"; Method = "$Method`_unicode_normalized"; Matches = @($matches) }
    }

    return [pscustomobject]@{ Status = "missing"; Method = $Method; Matches = @() }
}

function Find-UniqueByScopedFileName {
    param(
        [string]$Path,
        [System.Collections.Generic.List[string]]$PreferredRoots,
        [hashtable]$FileNameIndex,
        [string]$Method
    )

    $fileName = [System.IO.Path]::GetFileName((Normalize-RelPath $Path))
    $fileNameKey = Get-PathKey $fileName
    if ([string]::IsNullOrWhiteSpace($fileNameKey) -or -not $FileNameIndex.ContainsKey($fileNameKey)) {
        return [pscustomobject]@{ Status = "missing"; Method = $Method; Matches = @() }
    }

    $matches = New-Object System.Collections.Generic.List[string]
    $seen = New-Object System.Collections.Generic.HashSet[string]
    foreach ($full in $FileNameIndex[$fileNameKey]) {
        $relativeKey = Get-PathKey $full
        foreach ($root in $PreferredRoots) {
            $rootKey = Get-PathKey $root
            if ($relativeKey -eq $rootKey -or $relativeKey.StartsWith("$rootKey/")) {
                if ($seen.Add($full)) {
                    $matches.Add($full)
                }
                break
            }
        }
    }

    if ($matches.Count -eq 1) {
        return [pscustomobject]@{ Status = "resolved"; Method = $Method; Matches = @($matches[0]) }
    }
    if ($matches.Count -gt 1) {
        return [pscustomobject]@{ Status = "ambiguous"; Method = $Method; Matches = @($matches) }
    }
    return [pscustomobject]@{ Status = "missing"; Method = $Method; Matches = @() }
}

function Find-UniqueByTrailingSegments {
    param(
        [string]$Path,
        [System.Collections.Generic.List[string]]$PreferredRoots,
        [hashtable]$SuffixIndex,
        [int]$SegmentCount,
        [string]$Method
    )

    $parts = @((Normalize-RelPath $Path) -split "/" | Where-Object { -not [string]::IsNullOrWhiteSpace($_) })
    if ($parts.Count -lt $SegmentCount) {
        return [pscustomobject]@{ Status = "missing"; Method = $Method; Matches = @() }
    }
    $suffix = (($parts[($parts.Count - $SegmentCount)..($parts.Count - 1)] -join "/"))
    $suffixKey = Get-PathKey $suffix
    if (-not $SuffixIndex.ContainsKey($suffixKey)) {
        return [pscustomobject]@{ Status = "missing"; Method = $Method; Matches = @() }
    }

    $matches = New-Object System.Collections.Generic.List[string]
    $seen = New-Object System.Collections.Generic.HashSet[string]

    foreach ($relative in $SuffixIndex[$suffixKey]) {
        $relativeKey = Get-PathKey $relative
        foreach ($root in $PreferredRoots) {
            $rootKey = Get-PathKey $root
            if ($relativeKey -eq $rootKey -or $relativeKey.StartsWith("$rootKey/")) {
                if ($seen.Add($relative)) {
                    $matches.Add($relative)
                }
                break
            }
        }
    }

    if ($matches.Count -eq 1) {
        return [pscustomobject]@{ Status = "resolved"; Method = $Method; Matches = @($matches[0]) }
    }
    if ($matches.Count -gt 1) {
        return [pscustomobject]@{ Status = "ambiguous"; Method = $Method; Matches = @($matches) }
    }
    return [pscustomobject]@{ Status = "missing"; Method = $Method; Matches = @() }
}

function Resolve-MissingRow {
    param(
        [object]$Row,
        [hashtable]$ExactIndex,
        [hashtable]$NormIndex,
        [hashtable]$FileNameIndex,
        [hashtable]$SuffixIndexes,
        [System.Collections.Generic.HashSet[string]]$TopPrefixes
    )

    $linkPath = Normalize-RelPath $Row.linkPath
    $relationPath = Get-LinkPathFromRelationUrl $Row.relationUrl
    if (-not [string]::IsNullOrWhiteSpace($relationPath) -and [string]::IsNullOrWhiteSpace($linkPath)) {
        $linkPath = $relationPath
    }

    $fileCheckPath = $linkPath
    if (-not [string]::IsNullOrWhiteSpace($relationPath)) {
        $fileCheckPath = $relationPath
    }

    $fileLike = Test-ProbablyFileLike $Row $fileCheckPath
    if (-not $fileLike.IsFileLike) {
        return [pscustomobject]@{
            Status = "not_file_or_malformed"
            Method = $fileLike.Reason
            ResolvedRelativePath = ""
            CandidateRelativePaths = @()
        }
    }

    $preferredRoots = Get-PreferredRoots $Row $TopPrefixes
    $candidates = New-Object System.Collections.Generic.List[string]
    $candidateSeen = New-Object System.Collections.Generic.HashSet[string]

    Add-Candidate $candidates $candidateSeen $linkPath
    if (-not [string]::IsNullOrWhiteSpace($relationPath)) {
        Add-Candidate $candidates $candidateSeen $relationPath
    }

    $hasTopPrefix = Test-KnownTopPrefix $linkPath $TopPrefixes
    foreach ($root in $preferredRoots) {
        if (-not $hasTopPrefix) {
            Add-Candidate $candidates $candidateSeen (Join-Path $root $linkPath)
            if (-not [string]::IsNullOrWhiteSpace($relationPath)) {
                Add-Candidate $candidates $candidateSeen (Join-Path $root $relationPath)
            }
        }
    }

    $exact = Find-ByExactCandidates $candidates $ExactIndex $NormIndex "candidate_path"
    if ($exact.Status -eq "resolved") {
        return [pscustomobject]@{
            Status = "resolved"
            Method = $exact.Method
            ResolvedRelativePath = $exact.Matches[0]
            CandidateRelativePaths = @($exact.Matches)
        }
    }
    if ($exact.Status -eq "ambiguous") {
        return [pscustomobject]@{
            Status = "ambiguous"
            Method = $exact.Method
            ResolvedRelativePath = ""
            CandidateRelativePaths = @($exact.Matches)
        }
    }

    foreach ($segmentCount in @(4, 3, 2)) {
        $suffix = Find-UniqueByTrailingSegments $linkPath $preferredRoots $SuffixIndexes[$segmentCount] $segmentCount "unique_trailing_$segmentCount`_segments_in_preferred_root"
        if ($suffix.Status -eq "resolved") {
            return [pscustomobject]@{
                Status = "resolved"
                Method = $suffix.Method
                ResolvedRelativePath = $suffix.Matches[0]
                CandidateRelativePaths = @($suffix.Matches)
            }
        }
        if ($suffix.Status -eq "ambiguous") {
            return [pscustomobject]@{
                Status = "ambiguous"
                Method = $suffix.Method
                ResolvedRelativePath = ""
                CandidateRelativePaths = @($suffix.Matches)
            }
        }
    }

    $byName = Find-UniqueByScopedFileName $linkPath $preferredRoots $FileNameIndex "unique_filename_in_preferred_root"
    if ($byName.Status -eq "resolved") {
        return [pscustomobject]@{
            Status = "resolved"
            Method = $byName.Method
            ResolvedRelativePath = $byName.Matches[0]
            CandidateRelativePaths = @($byName.Matches)
        }
    }
    if ($byName.Status -eq "ambiguous") {
        return [pscustomobject]@{
            Status = "ambiguous"
            Method = $byName.Method
            ResolvedRelativePath = ""
            CandidateRelativePaths = @($byName.Matches)
        }
    }

    return [pscustomobject]@{
        Status = "confirmed_missing"
        Method = "no_exact_or_unique_scoped_match"
        ResolvedRelativePath = ""
        CandidateRelativePaths = @()
    }
}

function Export-CsvUtf8Bom {
    param(
        [object[]]$Rows,
        [string]$Path
    )
    $Rows | Export-Csv -LiteralPath $Path -NoTypeInformation -Encoding UTF8BOM
}

$missingCsvFull = [System.IO.Path]::GetFullPath($MissingCsv)
$wwwRootFull = [System.IO.Path]::GetFullPath($WwwRoot)
$outDirFull = [System.IO.Path]::GetFullPath($OutDir)

if (-not (Test-Path -LiteralPath $missingCsvFull)) {
    throw "MissingCsv not found: $missingCsvFull"
}
if (-not (Test-Path -LiteralPath $wwwRootFull)) {
    throw "WwwRoot not found: $wwwRootFull"
}
Ensure-Directory $outDirFull

Write-Host "Reading missing rows: $missingCsvFull"
$missingRows = @(Import-Csv -LiteralPath $missingCsvFull)

Write-Host "Indexing local www files: $wwwRootFull"
$exactIndex = @{}
$normIndex = @{}
$fileNameIndex = @{}
$suffixIndexes = @{
    2 = @{}
    3 = @{}
    4 = @{}
}
$topPrefixes = New-Object System.Collections.Generic.HashSet[string]
foreach ($dir in Get-ChildItem -LiteralPath $wwwRootFull -Directory -ErrorAction SilentlyContinue) {
    [void]$topPrefixes.Add($dir.Name.ToLowerInvariant())
}

$wwwFiles = @(Get-ChildItem -LiteralPath $wwwRootFull -Recurse -File -ErrorAction SilentlyContinue)
foreach ($file in $wwwFiles) {
    $relative = Get-RelativeFromFullPath $file.FullName $wwwRootFull
    Add-HashList $exactIndex ((Normalize-RelPath $relative).ToLowerInvariant()) $relative
    Add-HashList $normIndex (Get-PathKey $relative) $relative
    Add-HashList $fileNameIndex (Get-PathKey ([System.IO.Path]::GetFileName($relative))) $relative

    $parts = @($relative -split "/" | Where-Object { -not [string]::IsNullOrWhiteSpace($_) })
    foreach ($segmentCount in @(2, 3, 4)) {
        if ($parts.Count -ge $segmentCount) {
            $suffix = ($parts[($parts.Count - $segmentCount)..($parts.Count - 1)] -join "/")
            Add-HashList $suffixIndexes[$segmentCount] (Get-PathKey $suffix) $relative
        }
    }
}

Write-Host "Rechecking missing rows only: $($missingRows.Count)"
$results = New-Object System.Collections.Generic.List[object]
$i = 0
foreach ($row in $missingRows) {
    $i++
    if (($i % 500) -eq 0) {
        Write-Host "  checked $i / $($missingRows.Count)"
    }

    $resolution = Resolve-MissingRow $row $exactIndex $normIndex $fileNameIndex $suffixIndexes $topPrefixes
    $candidatePaths = @($resolution.CandidateRelativePaths)
    $resolvedFullPath = ""
    if ($resolution.Status -eq "resolved" -and -not [string]::IsNullOrWhiteSpace($resolution.ResolvedRelativePath)) {
        $resolvedFullPath = Join-Path $wwwRootFull ($resolution.ResolvedRelativePath -replace "/", [System.IO.Path]::DirectorySeparatorChar)
    }

    $results.Add([pscustomobject]@{
        id = $row.id
        department = $row.department
        mdRelativePath = $row.mdRelativePath
        articleUrl = $row.articleUrl
        relationUrl = $row.relationUrl
        linkPath = $row.linkPath
        articleBase = $row.articleBase
        extension = $row.extension
        recheckStatus = $resolution.Status
        recheckMethod = $resolution.Method
        resolvedRelativePath = $resolution.ResolvedRelativePath
        resolvedFullPath = $resolvedFullPath
        candidateCount = $candidatePaths.Count
        candidateRelativePaths = ($candidatePaths -join " | ")
    })
}

$reportCsv = Join-Path $outDirFull "missing_recheck_report.csv"
$resolvedCsv = Join-Path $outDirFull "missing_recheck_resolved.csv"
$confirmedMissingCsv = Join-Path $outDirFull "missing_recheck_confirmed_missing.csv"
$ambiguousCsv = Join-Path $outDirFull "missing_recheck_ambiguous.csv"
$notFileCsv = Join-Path $outDirFull "missing_recheck_not_file_or_malformed.csv"
$statusSummaryCsv = Join-Path $outDirFull "missing_recheck_status_summary.csv"
$departmentSummaryCsv = Join-Path $outDirFull "missing_recheck_by_department.csv"
$methodSummaryCsv = Join-Path $outDirFull "missing_recheck_by_method.csv"
$uniqueLinksCsv = Join-Path $outDirFull "missing_recheck_unique_links.csv"
$uniqueStatusSummaryCsv = Join-Path $outDirFull "missing_recheck_unique_status_summary.csv"
$duplicateLinksCsv = Join-Path $outDirFull "missing_recheck_duplicate_links.csv"
$summaryJson = Join-Path $outDirFull "missing_recheck_summary.json"

$allResults = @($results.ToArray())
$sep = [char]31
$uniqueRows = @(
    $allResults |
        Group-Object { @($_.department, $_.articleUrl, $_.relationUrl, $_.linkPath, $_.articleBase, $_.recheckStatus) -join $sep } |
        ForEach-Object {
            $first = $_.Group[0]
            [pscustomobject]@{
                department = $first.department
                articleUrl = $first.articleUrl
                relationUrl = $first.relationUrl
                linkPath = $first.linkPath
                articleBase = $first.articleBase
                extension = $first.extension
                recheckStatus = $first.recheckStatus
                recheckMethod = $first.recheckMethod
                resolvedRelativePath = $first.resolvedRelativePath
                candidateCount = $first.candidateCount
                candidateRelativePaths = $first.candidateRelativePaths
                duplicateRowCount = $_.Count
                mdRelativePaths = ((@($_.Group | ForEach-Object { $_.mdRelativePath }) | Sort-Object -Unique) -join " | ")
            }
        }
)

Export-CsvUtf8Bom $allResults $reportCsv
Export-CsvUtf8Bom @($allResults | Where-Object { $_.recheckStatus -eq "resolved" }) $resolvedCsv
Export-CsvUtf8Bom @($allResults | Where-Object { $_.recheckStatus -eq "confirmed_missing" }) $confirmedMissingCsv
Export-CsvUtf8Bom @($allResults | Where-Object { $_.recheckStatus -eq "ambiguous" }) $ambiguousCsv
Export-CsvUtf8Bom @($allResults | Where-Object { $_.recheckStatus -eq "not_file_or_malformed" }) $notFileCsv
Export-CsvUtf8Bom @($allResults | Group-Object recheckStatus | Sort-Object Name | Select-Object Name, Count) $statusSummaryCsv
Export-CsvUtf8Bom @($allResults | Group-Object department, recheckStatus | Sort-Object Name | Select-Object Name, Count) $departmentSummaryCsv
Export-CsvUtf8Bom @($allResults | Group-Object recheckMethod | Sort-Object Count -Descending | Select-Object Name, Count) $methodSummaryCsv
Export-CsvUtf8Bom $uniqueRows $uniqueLinksCsv
Export-CsvUtf8Bom @($uniqueRows | Group-Object recheckStatus | Sort-Object Name | Select-Object Name, Count) $uniqueStatusSummaryCsv
Export-CsvUtf8Bom @($uniqueRows | Where-Object { $_.duplicateRowCount -gt 1 }) $duplicateLinksCsv

$summary = [ordered]@{
    generatedAt = (Get-Date).ToString("s")
    sourceMissingCsv = $missingCsvFull
    wwwRoot = $wwwRootFull
    sourceMissingRows = $missingRows.Count
    localWwwFilesIndexed = $wwwFiles.Count
    recheckResolved = @($allResults | Where-Object { $_.recheckStatus -eq "resolved" }).Count
    recheckAmbiguous = @($allResults | Where-Object { $_.recheckStatus -eq "ambiguous" }).Count
    recheckNotFileOrMalformed = @($allResults | Where-Object { $_.recheckStatus -eq "not_file_or_malformed" }).Count
    recheckConfirmedMissing = @($allResults | Where-Object { $_.recheckStatus -eq "confirmed_missing" }).Count
    uniqueLinkRows = $uniqueRows.Count
    uniqueResolved = @($uniqueRows | Where-Object { $_.recheckStatus -eq "resolved" }).Count
    uniqueAmbiguous = @($uniqueRows | Where-Object { $_.recheckStatus -eq "ambiguous" }).Count
    uniqueNotFileOrMalformed = @($uniqueRows | Where-Object { $_.recheckStatus -eq "not_file_or_malformed" }).Count
    uniqueConfirmedMissing = @($uniqueRows | Where-Object { $_.recheckStatus -eq "confirmed_missing" }).Count
    duplicateLinkGroups = @($uniqueRows | Where-Object { $_.duplicateRowCount -gt 1 }).Count
    outputs = [ordered]@{
        reportCsv = $reportCsv
        resolvedCsv = $resolvedCsv
        confirmedMissingCsv = $confirmedMissingCsv
        ambiguousCsv = $ambiguousCsv
        notFileOrMalformedCsv = $notFileCsv
        statusSummaryCsv = $statusSummaryCsv
        departmentSummaryCsv = $departmentSummaryCsv
        methodSummaryCsv = $methodSummaryCsv
        uniqueLinksCsv = $uniqueLinksCsv
        uniqueStatusSummaryCsv = $uniqueStatusSummaryCsv
        duplicateLinksCsv = $duplicateLinksCsv
    }
}

$summary | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $summaryJson -Encoding UTF8

Write-Host "Done."
Write-Host "Resolved after recheck: $($summary.recheckResolved)"
Write-Host "Ambiguous after recheck: $($summary.recheckAmbiguous)"
Write-Host "Not file or malformed: $($summary.recheckNotFileOrMalformed)"
Write-Host "Confirmed missing: $($summary.recheckConfirmedMissing)"
Write-Host "Unique confirmed missing: $($summary.uniqueConfirmedMissing)"
Write-Host "Report: $reportCsv"
