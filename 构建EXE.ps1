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
    $virtualPython = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
    if (-not (Test-Path -LiteralPath $virtualPython -PathType Leaf)) {
        throw "未找到项目运行环境。请先运行一键启动.ps1。"
    }

    # Fail before touching any previous artifact or running a dependency install.
    Assert-ModelFiles -SourceRoot $PSScriptRoot
    $documents = @(Get-ReleaseFiles -SourceRoot $PSScriptRoot -Section full_documents)
    $assets = @(Get-ReleaseFiles -SourceRoot $PSScriptRoot -Section full_assets)
    $modelManifest = Get-ModelManifest -SourceRoot $PSScriptRoot
    $outputDirectory = Assert-ReleasePath -OutputRoot $OutputRoot -Path (Join-Path $OutputRoot "SeatSentinel")
    $outputExe = Join-Path $outputDirectory "SeatSentinel.exe"
    $running = @(Get-Process -Name "SeatSentinel" -ErrorAction SilentlyContinue | Where-Object {
        try { [IO.Path]::GetFullPath($_.Path) -eq $outputExe } catch { $false }
    })
    if ($running.Count -gt 0) {
        throw "输出目录内的 SeatSentinel 正在运行。请退出该实例，或指定其他 -OutputRoot。"
    }

    $buildLock = Join-Path $PSScriptRoot "requirements-build.lock"
    $dependencyFiles = @("requirements.txt", "requirements-build.txt", "requirements-runtime.lock", "requirements-build.lock")
    $requirementsHash = ($dependencyFiles | ForEach-Object {
        (Get-FileHash -LiteralPath (Join-Path $PSScriptRoot $_) -Algorithm SHA256).Hash
    }) -join ':'
    $marker = Join-Path $PSScriptRoot ".venv\.seat-sentinel-build-requirements.sha256"
    $installedHash = if (Test-Path -LiteralPath $marker) {
        ([string](Get-Content -LiteralPath $marker -Raw)).Trim()
    } else { "" }
    if ($installedHash -ne $requirementsHash) {
        Write-Host "==> 准备锁定版本的构建依赖" -ForegroundColor Cyan
        & $virtualPython -m pip install --disable-pip-version-check --no-cache-dir --retries 2 --timeout 30 -r $buildLock
        if ($LASTEXITCODE -ne 0) {
            & $virtualPython -m pip install --disable-pip-version-check --no-cache-dir --retries 3 --timeout 60 `
                --index-url "https://pypi.tuna.tsinghua.edu.cn/simple" -r $buildLock
        }
        if ($LASTEXITCODE -ne 0) { throw "安装构建依赖失败（退出码：$LASTEXITCODE）" }
        Set-Content -LiteralPath $marker -Value $requirementsHash -Encoding ASCII
    }
    & $virtualPython -m pip check
    if ($LASTEXITCODE -ne 0) { throw "项目依赖检查失败（退出码：$LASTEXITCODE）" }
    & $virtualPython "tools\validate_release.py" --environment
    if ($LASTEXITCODE -ne 0) { throw "构建环境与版本锁文件不一致。" }

    $stage = New-ReleaseStage -OutputRoot $OutputRoot
    $inputs = Join-Path $stage "inputs"
    $modelFiles = @("model-manifest.json")
    foreach ($model in $modelManifest.Models) {
        $modelFiles += "models/$($model.Name).xml"
        $modelFiles += "models/$($model.Name).bin"
    }
    Copy-ReleaseFiles -SourceRoot $PSScriptRoot -DestinationRoot $inputs -Files ($modelFiles + $assets)
    Assert-ModelFiles -SourceRoot $inputs
    $stagedDist = Join-Path $stage "dist"
    $work = Join-Path $stage "build"
    $spec = Join-Path $stage "spec"
    [void](New-Item -ItemType Directory -Path $spec -Force)
    $arguments = @("-m", "PyInstaller", "--noconfirm", "--clean", "--onedir", "--windowed",
        "--name", "SeatSentinel", "--icon", (Join-Path $inputs "assets\seatsentinel-icon.ico"),
        "--distpath", $stagedDist, "--workpath", $work, "--specpath", $spec,
        "--collect-all", "openvino", "--collect-all", "pystray", "--collect-all", "cv2_enumerate_cameras")
    foreach ($asset in $assets) {
        $assetPath = Join-Path $inputs $asset
        $assetDirectory = Split-Path -Parent $asset
        $arguments += @("--add-data", "$assetPath;$assetDirectory")
    }
    $arguments += @("--add-data", ((Join-Path $inputs "models") + ";models"), "app.py")
    Write-Host "==> 在独立暂存目录构建 EXE" -ForegroundColor Cyan
    & $virtualPython @arguments
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller 构建失败（退出码：$LASTEXITCODE）" }
    $stagedApplication = Join-Path $stagedDist "SeatSentinel"
    $stagedExe = Join-Path $stagedApplication "SeatSentinel.exe"
    if (-not (Test-Path -LiteralPath $stagedExe -PathType Leaf)) { throw "构建未生成 SeatSentinel.exe。" }
    Copy-ReleaseFiles -SourceRoot $PSScriptRoot -DestinationRoot $stagedApplication -Files $documents

    Write-Host "==> 在独立测试桌面验证暂存 EXE" -ForegroundColor Cyan
    & $virtualPython "tools\validate_app_lock.py" --packaged $stagedExe
    if ($LASTEXITCODE -ne 0) { throw "暂存 EXE 自检失败；原发行物保持不变。" }

    $archiveName = "SeatSentinel-Windows-x64.zip"
    $archive = Join-Path $stage $archiveName
    Compress-Archive -LiteralPath $stagedApplication -DestinationPath $archive -CompressionLevel Optimal
    Assert-ReleaseArchive -Archive $archive -PackageName "SeatSentinel"
    $hash = (Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash.ToLowerInvariant()
    $checksum = "$archive.sha256"
    Set-Content -LiteralPath $checksum -Value "$hash  $archiveName" -Encoding ASCII
    Publish-ReleaseArtifacts -OutputRoot $OutputRoot -Items @(
        [PSCustomObject]@{Source=$stagedApplication; Name="SeatSentinel"},
        [PSCustomObject]@{Source=$archive; Name=$archiveName},
        [PSCustomObject]@{Source=$checksum; Name="$archiveName.sha256"}
    )
    Remove-ReleaseStage -OutputRoot $OutputRoot -Stage $stage
    $stage = $null
    Write-Host "构建、自检和发布完成；旧版产物已保留备份。" -ForegroundColor Green
    Write-Host "EXE：$outputExe"
    Write-Host "压缩包：$(Join-Path $OutputRoot $archiveName)"
    if (-not $NoPause) { [void](Read-Host "按 Enter 键关闭窗口") }
}
catch {
    Write-Host "错误：$($_.Exception.Message)" -ForegroundColor Red
    if ($stage) { Write-Host "诊断暂存目录保留：$stage" }
    if (-not $NoPause) { [void](Read-Host "构建未完成。按 Enter 键关闭窗口") }
    exit 1
}
