[CmdletBinding()]
param(
    [switch]$VerifyModelsOnly
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

function Write-Step {
    param([Parameter(Mandatory = $true)][string]$Message)

    Write-Host ""
    Write-Host "==> $Message" -ForegroundColor Cyan
}

function Wait-OnError {
    Write-Host ""
    [void](Read-Host "启动未完成。按 Enter 键关闭窗口")
}

function Test-CommandSucceeded {
    param([Parameter(Mandatory = $true)][string]$Description)

    if ($LASTEXITCODE -ne 0) {
        throw "$Description（退出码：$LASTEXITCODE）"
    }
}

function Test-FileSha256 {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$ExpectedSha256
    )

    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        return $false
    }

    try {
        $actualSha256 = (
            Get-FileHash -LiteralPath $Path -Algorithm SHA256
        ).Hash
    }
    catch {
        return $false
    }

    return $actualSha256.Equals(
        $ExpectedSha256,
        [System.StringComparison]::OrdinalIgnoreCase
    )
}

function Download-ModelFile {
    param(
        [Parameter(Mandatory = $true)][string]$Url,
        [Parameter(Mandatory = $true)][string]$Destination,
        [Parameter(Mandatory = $true)][long]$MinimumBytes,
        [Parameter(Mandatory = $true)][string]$ExpectedSha256
    )

    if (
        (Test-FileSha256 `
            -Path $Destination `
            -ExpectedSha256 $ExpectedSha256)
    ) {
        return
    }

    $temporaryFile = "$Destination.download"
    try {
        Invoke-WebRequest `
            -Uri $Url `
            -OutFile $temporaryFile `
            -UseBasicParsing

        if (
            -not (Test-Path -LiteralPath $temporaryFile) -or
            ((Get-Item -LiteralPath $temporaryFile).Length -lt $MinimumBytes)
        ) {
            throw "下载的模型文件不完整：$Url"
        }

        if (
            -not (Test-FileSha256 `
                -Path $temporaryFile `
                -ExpectedSha256 $ExpectedSha256)
        ) {
            throw "模型文件 SHA-256 校验失败，已拒绝使用：$Url"
        }

        Move-Item `
            -LiteralPath $temporaryFile `
            -Destination $Destination `
            -Force
    }
    finally {
        if (Test-Path -LiteralPath $temporaryFile) {
            Remove-Item -LiteralPath $temporaryFile -Force
        }
    }
}

try {
    $Host.UI.RawUI.WindowTitle = "SeatSentinel"
    Set-Location -LiteralPath $PSScriptRoot

    Write-Host "SeatSentinel - 一键启动" -ForegroundColor Green
    Write-Host "首次运行会安装本地依赖并下载人脸检测模型。"

    $preferredPython = Join-Path `
        $env:LOCALAPPDATA `
        "Programs\Python\Python313\python.exe"
    $basePython = $null
    $basePythonArguments = @()

    if (Test-Path -LiteralPath $preferredPython) {
        $basePython = $preferredPython
    }
    else {
        $launcher = Get-Command "py.exe" -ErrorAction SilentlyContinue
        if ($null -eq $launcher) {
            throw "未找到 Python 3.13。请确认 Python 已正确安装。"
        }
        $basePython = $launcher.Source
        $basePythonArguments = @("-3.13")
    }

    $detectedVersion = & $basePython @basePythonArguments `
        -c "import sys; print('.'.join(map(str, sys.version_info[:3])))"
    Test-CommandSucceeded "无法运行 Python 3.13"
    if (-not $detectedVersion.StartsWith("3.13.")) {
        throw "需要 Python 3.13，当前检测到：$detectedVersion"
    }
    Write-Host "Python：$detectedVersion"

    $virtualEnvironment = Join-Path $PSScriptRoot ".venv"
    $virtualPython = Join-Path `
        $virtualEnvironment `
        "Scripts\python.exe"

    if (-not (Test-Path -LiteralPath $virtualPython)) {
        Write-Step "首次创建程序运行环境"
        & $basePython @basePythonArguments `
            -m venv $virtualEnvironment
        Test-CommandSucceeded "创建运行环境失败"
    }

    $requirementsFile = Join-Path $PSScriptRoot "requirements.txt"
    $dependencyMarker = Join-Path `
        $virtualEnvironment `
        ".seat-sentinel-requirements.sha256"
    $requirementsHash = (
        Get-FileHash -LiteralPath $requirementsFile -Algorithm SHA256
    ).Hash
    $installedHash = ""
    if (Test-Path -LiteralPath $dependencyMarker) {
        $markerContent = (
            Get-Content -LiteralPath $dependencyMarker -Raw
        )
        if ($null -ne $markerContent) {
            $installedHash = $markerContent.Trim()
        }
    }

    if ($installedHash -ne $requirementsHash) {
        Write-Step "首次安装或更新程序组件，请稍候"

        & $virtualPython -m pip install `
            --disable-pip-version-check `
            --retries 2 `
            --timeout 30 `
            -r $requirementsFile

        if ($LASTEXITCODE -ne 0) {
            Write-Host ""
            Write-Host (
                "PyPI 官方源连接失败，自动切换到清华镜像重试。"
            ) -ForegroundColor Yellow

            & $virtualPython -m pip install `
                --disable-pip-version-check `
                --retries 3 `
                --timeout 60 `
                --index-url `
                "https://pypi.tuna.tsinghua.edu.cn/simple" `
                -r $requirementsFile
        }

        Test-CommandSucceeded "安装程序组件失败"
        Set-Content `
            -LiteralPath $dependencyMarker `
            -Value $requirementsHash `
            -Encoding ASCII
    }

    $modelsDirectory = Join-Path $PSScriptRoot "models"
    if (-not (Test-Path -LiteralPath $modelsDirectory)) {
        [void](New-Item `
            -ItemType Directory `
            -Path $modelsDirectory)
    }

    $modelBaseUrl = (
        "https://storage.openvinotoolkit.org/repositories/" +
        "open_model_zoo/2022.1/models_bin/2/" +
        "face-detection-retail-0004/FP32"
    )
    $modelXml = Join-Path `
        $modelsDirectory `
        "face-detection-retail-0004.xml"
    $modelBin = Join-Path `
        $modelsDirectory `
        "face-detection-retail-0004.bin"
    $modelXmlSha256 = (
        "E1103759CF32B74AE3C2E84E9653DB5F" +
        "A0D69AC246DC1E17AC3B116EFF319459"
    )
    $modelBinSha256 = (
        "89349CE12DD21C5263FB302CD3FFD4B7" +
        "3C35EA12ED98AFF863D03A2CF3A32464"
    )

    $modelXmlIsValid = Test-FileSha256 `
        -Path $modelXml `
        -ExpectedSha256 $modelXmlSha256
    $modelBinIsValid = Test-FileSha256 `
        -Path $modelBin `
        -ExpectedSha256 $modelBinSha256

    if (-not $modelXmlIsValid -or -not $modelBinIsValid) {
        Write-Step "首次下载 Intel Open Model Zoo 人脸检测模型"
        Download-ModelFile `
            -Url "$modelBaseUrl/face-detection-retail-0004.xml" `
            -Destination $modelXml `
            -MinimumBytes 10000 `
            -ExpectedSha256 $modelXmlSha256
        Download-ModelFile `
            -Url "$modelBaseUrl/face-detection-retail-0004.bin" `
            -Destination $modelBin `
            -MinimumBytes 1000000 `
            -ExpectedSha256 $modelBinSha256
    }
    Write-Host "模型 SHA-256 校验通过。"

    if ($VerifyModelsOnly) {
        Write-Host "模型完整性检查完成，未启动 SeatSentinel。"
        exit 0
    }

    Write-Step "启动 SeatSentinel"
    Write-Host "程序运行期间按 Ctrl+C 可以安全退出。"
    Write-Host ""

    & $virtualPython (Join-Path $PSScriptRoot "app.py")
    Test-CommandSucceeded "程序异常退出"
}
catch {
    Write-Host ""
    Write-Host "错误：$($_.Exception.Message)" -ForegroundColor Red
    Wait-OnError
    exit 1
}
