[CmdletBinding()]
param(
    [switch]$NoPause,
    [string]$OutputRoot = ""
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest
$stage = $null

try {
    . (Join-Path $PSScriptRoot "tools\release_helpers.ps1")
    Set-Location -LiteralPath $PSScriptRoot
    if (-not $OutputRoot) { $OutputRoot = Join-Path $PSScriptRoot "dist" }
    $OutputRoot = [IO.Path]::GetFullPath($OutputRoot)
    $files = @(Get-ReleaseFiles -SourceRoot $PSScriptRoot -Section light_files)
    $configText = Get-Content -LiteralPath (Join-Path $PSScriptRoot "config.py") -Raw -Encoding UTF8
    $versionMatch = [regex]::Match($configText, '(?m)^APPLICATION_VERSION\s*=\s*"([0-9A-Za-z.-]+)"\s*$')
    if (-not $versionMatch.Success) { throw "无法读取应用版本。" }
    $packageName = "SeatSentinel-v$($versionMatch.Groups[1].Value)-Light"
    $stage = New-ReleaseStage -OutputRoot $OutputRoot
    $packageDirectory = Join-Path $stage $packageName
    Copy-ReleaseFiles -SourceRoot $PSScriptRoot -DestinationRoot $packageDirectory -Files $files

    $archiveName = "$packageName.zip"
    $archive = Join-Path $stage $archiveName
    Compress-Archive -LiteralPath $packageDirectory -DestinationPath $archive -CompressionLevel Optimal
    Assert-ReleaseArchive -Archive $archive -PackageName $packageName -ExpectedFiles $files
    $archiveHash = (Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash.ToLowerInvariant()
    $checksum = "$archive.sha256"
    Set-Content -LiteralPath $checksum -Value "$archiveHash  $archiveName" -Encoding ASCII
    Publish-ReleaseArtifacts -OutputRoot $OutputRoot -Items @(
        [PSCustomObject]@{Source=$archive; Name=$archiveName},
        [PSCustomObject]@{Source=$checksum; Name="$archiveName.sha256"}
    )
    Remove-ReleaseStage -OutputRoot $OutputRoot -Stage $stage
    $stage = $null
    Write-Host "轻量版已按发布白名单构建；旧版产物已保留备份。" -ForegroundColor Green
    Write-Host "压缩包：$(Join-Path $OutputRoot $archiveName)"
    Write-Host "SHA-256：$archiveHash"
    if (-not $NoPause) { [void](Read-Host "按 Enter 键关闭窗口") }
}
catch {
    Write-Host "错误：$($_.Exception.Message)" -ForegroundColor Red
    if ($stage) { Write-Host "诊断暂存目录保留：$stage" }
    if (-not $NoPause) { [void](Read-Host "构建未完成。按 Enter 键关闭窗口") }
    exit 1
}
