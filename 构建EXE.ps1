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
    $Host.UI.RawUI.WindowTitle = "构建 SeatSentinel EXE"
    Set-Location -LiteralPath $PSScriptRoot

    $virtualPython = Join-Path `
        $PSScriptRoot `
        ".venv\Scripts\python.exe"
    if (-not (Test-Path -LiteralPath $virtualPython)) {
        throw "未找到项目运行环境。请先运行一键启动.ps1。"
    }

    $modelXml = Join-Path `
        $PSScriptRoot `
        "models\face-detection-retail-0004.xml"
    $modelBin = Join-Path `
        $PSScriptRoot `
        "models\face-detection-retail-0004.bin"
    if (
        -not (Test-Path -LiteralPath $modelXml) -or
        -not (Test-Path -LiteralPath $modelBin)
    ) {
        throw "未找到人脸检测模型。请先运行一键启动.ps1。"
    }

    Write-Host "==> 检查项目依赖" -ForegroundColor Cyan
    & $virtualPython -m pip check
    if ($LASTEXITCODE -ne 0) {
        throw "项目依赖检查失败（退出码：$LASTEXITCODE）"
    }

    Write-Host ""
    Write-Host "==> 构建无控制台托盘 EXE，请稍候" -ForegroundColor Cyan
    & $virtualPython -m PyInstaller `
        --noconfirm `
        --clean `
        --onedir `
        --windowed `
        --name "SeatSentinel" `
        --collect-all "openvino" `
        --collect-all "pystray" `
        --collect-all "cv2_enumerate_cameras" `
        --add-data "$PSScriptRoot\models;models" `
        "app.py"
    if ($LASTEXITCODE -ne 0) {
        throw "PyInstaller 构建失败（退出码：$LASTEXITCODE）"
    }

    $outputDirectory = Join-Path `
        $PSScriptRoot `
        "dist\SeatSentinel"
    $outputExe = Join-Path $outputDirectory "SeatSentinel.exe"
    if (-not (Test-Path -LiteralPath $outputExe)) {
        throw "构建完成但未找到 SeatSentinel.exe"
    }

    $distributionDocuments = @(
        "README.md",
        "LICENSE",
        "PRIVACY.md",
        "SECURITY.md",
        "THIRD_PARTY_NOTICES.md"
    )
    foreach ($documentName in $distributionDocuments) {
        Copy-Item `
            -LiteralPath (Join-Path $PSScriptRoot $documentName) `
            -Destination (Join-Path $outputDirectory $documentName) `
            -Force
    }
    $documentationAssets = Join-Path $PSScriptRoot "docs"
    if (Test-Path -LiteralPath $documentationAssets) {
        Copy-Item `
            -LiteralPath $documentationAssets `
            -Destination (Join-Path $outputDirectory "docs") `
            -Recurse `
            -Force
    }
    Copy-Item `
        -LiteralPath (Join-Path $PSScriptRoot "打开调试界面.cmd") `
        -Destination (Join-Path $outputDirectory "打开调试界面.cmd") `
        -Force

    Write-Host ""
    Write-Host "==> 执行不访问摄像头的打包自检" -ForegroundColor Cyan
    $selfTest = Start-Process `
        -FilePath $outputExe `
        -ArgumentList "--self-test" `
        -Wait `
        -PassThru `
        -WindowStyle Hidden
    if ($selfTest.ExitCode -ne 0) {
        $logPath = Join-Path `
            $env:LOCALAPPDATA `
            "SeatSentinel\logs\seat-sentinel.log"
        throw "打包自检失败，请查看日志：$logPath"
    }

    $archivePath = Join-Path `
        $PSScriptRoot `
        "dist\SeatSentinel-Windows-x64.zip"
    if (Test-Path -LiteralPath $archivePath) {
        Remove-Item -LiteralPath $archivePath -Force
    }
    Compress-Archive `
        -Path $outputDirectory `
        -DestinationPath $archivePath `
        -CompressionLevel Optimal

    # PyInstaller 的 build 目录仅包含构建中间文件，其中的 EXE 不能直接运行。
    # 成功生成并验证 dist 后将其清理，避免误点。
    $buildDirectory = Join-Path $PSScriptRoot "build"
    if (Test-Path -LiteralPath $buildDirectory) {
        $resolvedBuildDirectory = (Resolve-Path `
            -LiteralPath $buildDirectory).Path
        $expectedBuildDirectory = [System.IO.Path]::GetFullPath(
            $buildDirectory
        )
        if ($resolvedBuildDirectory -ne $expectedBuildDirectory) {
            throw "构建中间目录路径校验失败，未执行清理。"
        }
        Remove-Item `
            -LiteralPath $resolvedBuildDirectory `
            -Recurse `
            -Force
    }

    Write-Host ""
    Write-Host "构建及自检成功。" -ForegroundColor Green
    Write-Host "EXE：$outputExe"
    Write-Host "压缩包：$archivePath"
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
