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
    Write-Host "首次运行会安装本地依赖并下载本地人脸检测/识别模型。"

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

    $modelRepositoryUrl = (
        "https://storage.openvinotoolkit.org/repositories/" +
        "open_model_zoo/2022.1/models_bin/2"
    )
    $modelDefinitions = @(
        [PSCustomObject]@{
            Name = "face-detection-retail-0004"
            Label = "人脸检测"
            XmlSha256 = (
                "E1103759CF32B74AE3C2E84E9653DB5F" +
                "A0D69AC246DC1E17AC3B116EFF319459"
            )
            BinSha256 = (
                "89349CE12DD21C5263FB302CD3FFD4B7" +
                "3C35EA12ED98AFF863D03A2CF3A32464"
            )
            XmlMinimumBytes = 10000
            BinMinimumBytes = 1000000
        },
        [PSCustomObject]@{
            Name = "landmarks-regression-retail-0009"
            Label = "人脸关键点"
            XmlSha256 = (
                "8EDE1C8A94BFF1C0DDDA96F938CB8722" +
                "49BD0E1E33E77315498C8A8F17470AC1"
            )
            BinSha256 = (
                "71199E8D6DF4583C3BA4AD8EAB013F36" +
                "995B9FEF2DD6D85D86C2CC2322803955"
            )
            XmlMinimumBytes = 50000
            BinMinimumBytes = 700000
        },
        [PSCustomObject]@{
            Name = "face-reidentification-retail-0095"
            Label = "本人人脸特征"
            XmlSha256 = (
                "9148EB0E6578807B073F2A90649C7015" +
                "66A277DF1A2086E769C2CB263CC66B86"
            )
            BinSha256 = (
                "C0A0ACB57503ACB0B04A9AA3B1A6DA7" +
                "165C799D0DC2A462AD6B081A5CD1BC908"
            )
            XmlMinimumBytes = 300000
            BinMinimumBytes = 4000000
        }
    )

    foreach ($modelDefinition in $modelDefinitions) {
        $modelName = $modelDefinition.Name
        $modelBaseUrl = (
            "$modelRepositoryUrl/$modelName/FP32"
        )
        $modelXml = Join-Path `
            $modelsDirectory `
            "$modelName.xml"
        $modelBin = Join-Path `
            $modelsDirectory `
            "$modelName.bin"
        if (
            -not (Test-FileSha256 `
                -Path $modelXml `
                -ExpectedSha256 $modelDefinition.XmlSha256) -or
            -not (Test-FileSha256 `
                -Path $modelBin `
                -ExpectedSha256 $modelDefinition.BinSha256)
        ) {
            Write-Step (
                "下载 Intel Open Model Zoo $($modelDefinition.Label)模型"
            )
            Download-ModelFile `
                -Url "$modelBaseUrl/$modelName.xml" `
                -Destination $modelXml `
                -MinimumBytes $modelDefinition.XmlMinimumBytes `
                -ExpectedSha256 $modelDefinition.XmlSha256
            Download-ModelFile `
                -Url "$modelBaseUrl/$modelName.bin" `
                -Destination $modelBin `
                -MinimumBytes $modelDefinition.BinMinimumBytes `
                -ExpectedSha256 $modelDefinition.BinSha256
        }
    }
    Write-Host "全部模型 SHA-256 校验通过。"

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
