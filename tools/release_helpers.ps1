# Shared manifest, integrity and staged-publication helpers (PowerShell 5.1+).
Set-StrictMode -Version Latest

function Test-FileSha256 {
    param([string]$Path, [string]$ExpectedSha256)
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { return $false }
    try { $actual = (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash }
    catch { return $false }
    return $actual.Equals($ExpectedSha256, [StringComparison]::OrdinalIgnoreCase)
}

function Get-ModelManifest {
    param([Parameter(Mandatory = $true)][string]$SourceRoot)
    $manifest = Get-Content -LiteralPath (Join-Path $SourceRoot 'model-manifest.json') -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($manifest.SchemaVersion -ne 1 -or $manifest.Models.Count -ne 3 -or
        $manifest.RepositoryUrl -notmatch '^https://storage\.openvinotoolkit\.org/') {
        throw 'Invalid model manifest.'
    }
    $names = @{}
    foreach ($model in $manifest.Models) {
        if ($model.Name -notmatch '^[a-z0-9-]+$' -or $names.ContainsKey($model.Name) -or
            $model.XmlSha256 -notmatch '^[A-Fa-f0-9]{64}$' -or
            $model.BinSha256 -notmatch '^[A-Fa-f0-9]{64}$' -or
            $model.XmlMinimumBytes -le 0 -or $model.BinMinimumBytes -le 0) {
            throw 'Invalid or duplicate model entry.'
        }
        $names[$model.Name] = $true
    }
    return $manifest
}

function Assert-ModelFiles {
    param([Parameter(Mandatory = $true)][string]$SourceRoot)
    $manifest = Get-ModelManifest -SourceRoot $SourceRoot
    $invalid = @()
    foreach ($model in $manifest.Models) {
        foreach ($extension in @('xml', 'bin')) {
            $fileName = "$($model.Name).$extension"
            $expected = if ($extension -eq 'xml') { $model.XmlSha256 } else { $model.BinSha256 }
            if (-not (Test-FileSha256 -Path (Join-Path $SourceRoot "models\$fileName") -ExpectedSha256 $expected)) {
                $invalid += $fileName
            }
        }
    }
    if ($invalid.Count -gt 0) { throw ('Missing or invalid model SHA-256: ' + ($invalid -join ', ')) }
}

function Assert-ReleasePath {
    param([Parameter(Mandatory = $true)][string]$OutputRoot,
          [Parameter(Mandatory = $true)][string]$Path)
    $root = [IO.Path]::GetFullPath($OutputRoot).TrimEnd('\', '/')
    $target = [IO.Path]::GetFullPath($Path)
    if (-not $target.StartsWith($root + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {
        throw "Release path is outside its output root: $target"
    }
    # Reject junctions/symlinks in every existing ancestor before a directory
    # move or recursive removal, rather than relying on lexical containment.
    $ancestor = $target
    while ($ancestor) {
        if (Test-Path -LiteralPath $ancestor) {
            $item = Get-Item -LiteralPath $ancestor -Force
            if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) {
                throw "Release paths cannot traverse reparse points: $ancestor"
            }
        }
        $parent = Split-Path -Parent $ancestor
        if ($parent -eq $ancestor) { break }
        $ancestor = $parent
    }
    return $target
}

function Get-ReleaseFilePath {
    param([string]$Root, [string]$RelativePath)
    if ([IO.Path]::IsPathRooted($RelativePath) -or
        $RelativePath -match '(^|[\\/])\.\.?([\\/]|$)' -or $RelativePath.Contains(':')) {
        throw "Invalid release manifest path: $RelativePath"
    }
    return Assert-ReleasePath -OutputRoot $Root -Path (Join-Path $Root $RelativePath)
}

function Get-ReleaseFiles {
    param([string]$SourceRoot,
          [ValidateSet('light_files', 'full_documents', 'full_assets')][string]$Section)
    $manifest = Get-Content -LiteralPath (Join-Path $SourceRoot 'release-manifest.json') -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($manifest.schema_version -ne 1) { throw 'Invalid release manifest version.' }
    $seen = @{}
    foreach ($relative in $manifest.$Section) {
        $source = Get-ReleaseFilePath -Root $SourceRoot -RelativePath $relative
        if ($seen.ContainsKey($relative) -or -not (Test-Path -LiteralPath $source -PathType Leaf)) {
            throw "Missing or duplicate release file: $relative"
        }
        $seen[$relative] = $true
        Write-Output $relative
    }
}

function Copy-ReleaseFiles {
    param([string]$SourceRoot, [string]$DestinationRoot, [string[]]$Files)
    foreach ($relative in $Files) {
        $source = Get-ReleaseFilePath -Root $SourceRoot -RelativePath $relative
        $destination = Get-ReleaseFilePath -Root $DestinationRoot -RelativePath $relative
        [void](New-Item -ItemType Directory -Path (Split-Path -Parent $destination) -Force)
        Copy-Item -LiteralPath $source -Destination $destination -Force
    }
}

function New-ReleaseStage {
    param([Parameter(Mandatory = $true)][string]$OutputRoot)
    $root = [IO.Path]::GetFullPath($OutputRoot)
    $stage = Assert-ReleasePath -OutputRoot $root -Path (Join-Path $root ('.seatsentinel-stage-' + [guid]::NewGuid().ToString('N')))
    [void](New-Item -ItemType Directory -Path $stage -Force)
    return $stage
}

function Assert-ReleaseArchive {
    param([string]$Archive, [string]$PackageName, [string[]]$ExpectedFiles = @())
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $zip = [IO.Compression.ZipFile]::OpenRead($Archive)
    try {
        $files = @()
        foreach ($entry in $zip.Entries) {
            $name = $entry.FullName.Replace('\', '/')
            if (-not $name.StartsWith($PackageName + '/', [StringComparison]::Ordinal) -or
                $name -match '(^|/)\.\.(/|$)' -or $name.Contains(':')) {
                throw "Unsafe ZIP entry: $name"
            }
            if ($name.EndsWith('/')) { continue }
            $files += $name.Substring($PackageName.Length + 1)
            $stream = $entry.Open()
            try { $stream.CopyTo([IO.Stream]::Null) } finally { $stream.Dispose() }
        }
        if ($files.Count -eq 0) { throw 'The release archive is empty.' }
        if ($ExpectedFiles.Count -gt 0) {
            $expected = @($ExpectedFiles | ForEach-Object { $_.Replace('\', '/') } | Sort-Object)
            $actual = @($files | Sort-Object)
            if (@(Compare-Object -ReferenceObject $expected -DifferenceObject $actual).Count -ne 0) {
                throw 'ZIP contents differ from the explicit release manifest.'
            }
        }
    }
    finally { $zip.Dispose() }
}

function Remove-ReleaseStage {
    param([string]$OutputRoot, [string]$Stage)
    $target = Assert-ReleasePath -OutputRoot $OutputRoot -Path $Stage
    if ((Split-Path -Leaf $target) -notmatch '^\.seatsentinel-stage-[a-f0-9]{32}$' -or
        (Split-Path -Parent $target) -ne [IO.Path]::GetFullPath($OutputRoot).TrimEnd('\', '/')) {
        throw 'Refusing to remove an unrecognized staging directory.'
    }
    if (Test-Path -LiteralPath $target) { Remove-Item -LiteralPath $target -Recurse -Force }
}

function Publish-ReleaseArtifacts {
    param([string]$OutputRoot, [object[]]$Items)
    $plans = @()
    foreach ($item in $Items) {
        $source = Assert-ReleasePath -OutputRoot $OutputRoot -Path $item.Source
        $destination = Get-ReleaseFilePath -Root $OutputRoot -RelativePath $item.Name
        if (-not (Test-Path -LiteralPath $source)) { throw "Missing staged artifact: $source" }
        $plans += [PSCustomObject]@{Source=$source; Destination=$destination; Name=$item.Name; Backup=$null; Published=$false}
    }
    $backupRoot = Assert-ReleasePath -OutputRoot $OutputRoot -Path (
        Join-Path $OutputRoot ('backups\' + (Get-Date -Format 'yyyyMMdd-HHmmss') + '-' + [guid]::NewGuid().ToString('N')))
    try {
        foreach ($plan in $plans) {
            if (Test-Path -LiteralPath $plan.Destination) {
                [void](New-Item -ItemType Directory -Path $backupRoot -Force)
                $backup = Get-ReleaseFilePath -Root $backupRoot -RelativePath $plan.Name
                [void](Assert-ReleasePath -OutputRoot $OutputRoot -Path $plan.Destination)
                Move-Item -LiteralPath $plan.Destination -Destination $backup
                $plan.Backup = $backup
            }
            [void](Assert-ReleasePath -OutputRoot $OutputRoot -Path $plan.Source)
            Move-Item -LiteralPath $plan.Source -Destination $plan.Destination
            $plan.Published = $true
        }
    }
    catch {
        $originalFailure = $_
        for ($index = $plans.Count - 1; $index -ge 0; $index--) {
            $plan = $plans[$index]
            if ($plan.Published) {
                [void](Assert-ReleasePath -OutputRoot $OutputRoot -Path $plan.Destination)
                Move-Item -LiteralPath $plan.Destination -Destination $plan.Source
            }
            if ($null -ne $plan.Backup) {
                [void](Assert-ReleasePath -OutputRoot $OutputRoot -Path $plan.Backup)
                Move-Item -LiteralPath $plan.Backup -Destination $plan.Destination
            }
        }
        throw $originalFailure
    }
    if (Test-Path -LiteralPath $backupRoot) { Write-Host "Previous artifacts retained: $backupRoot" }
}
