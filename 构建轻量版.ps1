[CmdletBinding()]
param(
    [switch]$NoPause
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

function Wait-OnError {
    Write-Host ""
    if (-not $NoPause) {
        [void](Read-Host "构建未完成。按 Enter 键关闭窗口")
    }
}

try {
    $Host.UI.RawUI.WindowTitle = "构建 SeatSentinel 轻量联网版"
    Set-Location -LiteralPath $PSScriptRoot

    $configPath = Join-Path $PSScriptRoot "config.py"
    $configText = Get-Content -LiteralPath $configPath -Raw
    $versionMatch = [regex]::Match(
        $configText,
        '(?m)^APPLICATION_VERSION\s*=\s*"([^"]+)"\s*$'
    )
    if (-not $versionMatch.Success) {
        throw "无法从 config.py 读取应用版本。"
    }

    $version = $versionMatch.Groups[1].Value
    $packageName = "SeatSentinel-v$version-Light"
    $distDirectory = Join-Path $PSScriptRoot "dist"
    $stageDirectory = Join-Path `
        $distDirectory `
        ".light-package-stage"
    $packageDirectory = Join-Path $stageDirectory $packageName
    $archivePath = Join-Path `
        $distDirectory `
        "$packageName.zip"
    $checksumPath = "$archivePath.sha256"

    [void](New-Item `
        -ItemType Directory `
        -Path $distDirectory `
        -Force)

    $expectedStageDirectory = [IO.Path]::GetFullPath($stageDirectory)
    if (Test-Path -LiteralPath $stageDirectory) {
        $resolvedStageDirectory = (
            Resolve-Path -LiteralPath $stageDirectory
        ).Path
        if ($resolvedStageDirectory -ne $expectedStageDirectory) {
            throw "轻量版暂存目录路径校验失败，未执行清理。"
        }
        Remove-Item `
            -LiteralPath $resolvedStageDirectory `
            -Recurse `
            -Force
    }
    [void](New-Item `
        -ItemType Directory `
        -Path $packageDirectory `
        -Force)

    $rootFiles = @(
        "安装并启动.cmd",
        "一键启动.ps1",
        "requirements.txt",
        "settings.example.json",
        "README.md",
        "LICENSE",
        "PRIVACY.md",
        "SECURITY.md",
        "THIRD_PARTY_NOTICES.md"
    )
    foreach ($fileName in $rootFiles) {
        $sourcePath = Join-Path $PSScriptRoot $fileName
        if (-not (Test-Path -LiteralPath $sourcePath -PathType Leaf)) {
            throw "轻量版缺少必需文件：$fileName"
        }
        Copy-Item `
            -LiteralPath $sourcePath `
            -Destination (Join-Path $packageDirectory $fileName)
    }

    $pythonFiles = Get-ChildItem `
        -LiteralPath $PSScriptRoot `
        -File `
        -Filter "*.py"
    foreach ($pythonFile in $pythonFiles) {
        Copy-Item `
            -LiteralPath $pythonFile.FullName `
            -Destination (Join-Path $packageDirectory $pythonFile.Name)
    }

    foreach ($directoryName in @("assets", "docs")) {
        $sourceDirectory = Join-Path $PSScriptRoot $directoryName
        if (-not (Test-Path -LiteralPath $sourceDirectory)) {
            throw "轻量版缺少必需目录：$directoryName"
        }
        Copy-Item `
            -LiteralPath $sourceDirectory `
            -Destination (Join-Path $packageDirectory $directoryName) `
            -Recurse
    }

    $packageModels = Join-Path $packageDirectory "models"
    [void](New-Item -ItemType Directory -Path $packageModels)
    $modelsMarker = Join-Path $PSScriptRoot "models\.gitkeep"
    if (Test-Path -LiteralPath $modelsMarker) {
        Copy-Item `
            -LiteralPath $modelsMarker `
            -Destination (Join-Path $packageModels ".gitkeep")
    }

    foreach ($outputPath in @($archivePath, $checksumPath)) {
        if (Test-Path -LiteralPath $outputPath) {
            Remove-Item -LiteralPath $outputPath -Force
        }
    }

    Write-Host "==> 压缩轻量联网版" -ForegroundColor Cyan
    Compress-Archive `
        -Path $packageDirectory `
        -DestinationPath $archivePath `
        -CompressionLevel Optimal

    $archiveHash = (
        Get-FileHash -LiteralPath $archivePath -Algorithm SHA256
    ).Hash.ToLowerInvariant()
    Set-Content `
        -LiteralPath $checksumPath `
        -Value "$archiveHash  $packageName.zip" `
        -Encoding ASCII

    Remove-Item `
        -LiteralPath $expectedStageDirectory `
        -Recurse `
        -Force

    Write-Host ""
    Write-Host "轻量联网版构建成功。" -ForegroundColor Green
    Write-Host "压缩包：$archivePath"
    Write-Host "校验文件：$checksumPath"
    Write-Host "SHA-256：$archiveHash"
    Write-Host ""
    if (-not $NoPause) {
        [void](Read-Host "按 Enter 键关闭窗口")
    }
}
catch {
    Write-Host ""
    Write-Host "错误：$($_.Exception.Message)" -ForegroundColor Red
    Wait-OnError
    exit 1
}
