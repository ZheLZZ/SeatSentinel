[CmdletBinding()]
param(
    [switch]$VerifyModelsOnly,
    [switch]$InstallPythonIfMissing
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$PythonVersion = "3.13.12"
$PythonInstallerUrl = (
    "https://www.python.org/ftp/python/$PythonVersion/" +
    "python-$PythonVersion-amd64.exe"
)
$PythonInstallerSha256 = (
    "96159FCB523AE404B707186A75B4104E" +
    "E23851E476A5E838E14584CF1E03F981"
)

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

function Download-VerifiedFile {
    param(
        [Parameter(Mandatory = $true)][string]$Label,
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

    $destinationDirectory = Split-Path -Parent $Destination
    if (-not (Test-Path -LiteralPath $destinationDirectory)) {
        [void](New-Item `
            -ItemType Directory `
            -Path $destinationDirectory `
            -Force)
    }

    $temporaryFile = "$Destination.download"
    $lastFailure = $null
    for ($attempt = 1; $attempt -le 3; $attempt++) {
        try {
            if (Test-Path -LiteralPath $temporaryFile) {
                Remove-Item -LiteralPath $temporaryFile -Force
            }

            Write-Host "$Label（第 $attempt/3 次）"
            Invoke-WebRequest `
                -Uri $Url `
                -OutFile $temporaryFile `
                -UseBasicParsing

            if (
                -not (Test-Path -LiteralPath $temporaryFile) -or
                ((Get-Item -LiteralPath $temporaryFile).Length -lt $MinimumBytes)
            ) {
                throw "下载文件不完整"
            }

            if (
                -not (Test-FileSha256 `
                    -Path $temporaryFile `
                    -ExpectedSha256 $ExpectedSha256)
            ) {
                throw "SHA-256 校验失败，已拒绝使用"
            }

            Move-Item `
                -LiteralPath $temporaryFile `
                -Destination $Destination `
                -Force
            return
        }
        catch {
            $lastFailure = $_.Exception.Message
            if ($attempt -lt 3) {
                Write-Host (
                    "下载未完成：$lastFailure，稍后自动重试。"
                ) -ForegroundColor Yellow
                Start-Sleep -Seconds (2 * $attempt)
            }
        }
        finally {
            if (Test-Path -LiteralPath $temporaryFile) {
                Remove-Item -LiteralPath $temporaryFile -Force
            }
        }
    }

    throw "$Label 失败：$lastFailure`n下载地址：$Url"
}

function Get-CompatiblePythonVersion {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [string[]]$Arguments = @()
    )

    try {
        $probe = & $Path @Arguments `
            -c (
                "import struct, sys; " +
                "print('.'.join(map(str, sys.version_info[:3])) " +
                "+ '|' + str(struct.calcsize('P') * 8))"
            ) 2>$null
        if (
            $LASTEXITCODE -eq 0 -and
            $probe -match '^3\.13\.\d+\|64$'
        ) {
            return ($probe -split '\|')[0]
        }
    }
    catch {
        return $null
    }

    return $null
}

function Install-ManagedPython {
    param(
        [Parameter(Mandatory = $true)][string]$PythonHome,
        [Parameter(Mandatory = $true)][string]$RuntimeDirectory
    )

    Write-Step "准备 SeatSentinel 专用 Python $PythonVersion"
    Write-Host "仅安装到当前用户的 SeatSentinel 目录。"
    Write-Host "不会修改 PATH、文件关联或创建系统快捷方式。"

    $downloadsDirectory = Join-Path $RuntimeDirectory "downloads"
    [void](New-Item `
        -ItemType Directory `
        -Path $downloadsDirectory `
        -Force)
    $installerPath = Join-Path `
        $downloadsDirectory `
        "python-$PythonVersion-amd64.exe"
    Download-VerifiedFile `
        -Label "下载 Python.org 官方 Python 安装包" `
        -Url $PythonInstallerUrl `
        -Destination $installerPath `
        -MinimumBytes 25000000 `
        -ExpectedSha256 $PythonInstallerSha256

    $installLog = Join-Path $downloadsDirectory "python-install.log"
    $installerArguments = @(
        "/passive",
        "/log", "`"$installLog`"",
        "InstallAllUsers=0",
        "TargetDir=`"$PythonHome`"",
        "AssociateFiles=0",
        "CompileAll=0",
        "PrependPath=0",
        "AppendPath=0",
        "Shortcuts=0",
        "Include_doc=0",
        "Include_debug=0",
        "Include_dev=1",
        "Include_exe=1",
        "Include_launcher=0",
        "InstallLauncherAllUsers=0",
        "Include_lib=1",
        "Include_pip=1",
        "Include_symbols=0",
        "Include_tcltk=1",
        "Include_test=0",
        "Include_tools=0"
    )
    $installer = Start-Process `
        -FilePath $installerPath `
        -ArgumentList $installerArguments `
        -Wait `
        -PassThru
    if ($installer.ExitCode -notin @(0, 3010)) {
        throw (
            "Python 安装失败（退出码：$($installer.ExitCode)）。" +
            "安装日志：$installLog"
        )
    }

    $managedPython = Join-Path $PythonHome "python.exe"
    $installedVersion = Get-CompatiblePythonVersion -Path $managedPython
    if ($null -eq $installedVersion) {
        throw "Python 安装完成，但未找到可用的 64 位 Python 3.13。"
    }

    Remove-Item -LiteralPath $installerPath -Force
    return $managedPython
}

try {
    $Host.UI.RawUI.WindowTitle = "SeatSentinel"
    Set-Location -LiteralPath $PSScriptRoot

    Write-Host "SeatSentinel - 一键启动" -ForegroundColor Green
    Write-Host "首次运行会准备本地运行环境并下载本地人脸检测/识别模型。"

    [Net.ServicePointManager]::SecurityProtocol = (
        [Net.ServicePointManager]::SecurityProtocol -bor
        [Net.SecurityProtocolType]::Tls12
    )

    if (-not [Environment]::Is64BitOperatingSystem) {
        throw "SeatSentinel 仅支持 64 位 Windows。"
    }

    $runtimeDirectory = Join-Path `
        $env:LOCALAPPDATA `
        "SeatSentinel\runtime"
    $managedPythonHome = Join-Path `
        $runtimeDirectory `
        "Python313"
    $managedPython = Join-Path $managedPythonHome "python.exe"

    $preferredPython = Join-Path `
        $env:LOCALAPPDATA `
        "Programs\Python\Python313\python.exe"
    $basePython = $null
    $basePythonArguments = @()

    $pythonCandidates = @(
        [PSCustomObject]@{
            Path = $managedPython
            Arguments = @()
        },
        [PSCustomObject]@{
            Path = $preferredPython
            Arguments = @()
        }
    )
    $launcher = Get-Command "py.exe" -ErrorAction SilentlyContinue
    if ($null -ne $launcher) {
        $pythonCandidates += [PSCustomObject]@{
            Path = $launcher.Source
            Arguments = @("-3.13")
        }
    }

    $detectedVersion = $null
    foreach ($candidate in $pythonCandidates) {
        if (-not (Test-Path -LiteralPath $candidate.Path)) {
            continue
        }
        $candidateArguments = @($candidate.Arguments)
        $candidateVersion = Get-CompatiblePythonVersion `
            -Path $candidate.Path `
            -Arguments $candidateArguments
        if ($null -ne $candidateVersion) {
            $basePython = $candidate.Path
            $basePythonArguments = $candidateArguments
            $detectedVersion = $candidateVersion
            break
        }
    }

    if ($null -eq $basePython) {
        if (-not $InstallPythonIfMissing) {
            Write-Host ""
            Write-Host "未找到 64 位 Python 3.13。" -ForegroundColor Yellow
            $answer = Read-Host (
                "是否下载经校验的官方 Python $PythonVersion，" +
                "并安装到 SeatSentinel 用户目录？[Y/n]"
            )
            if ($answer -match '^(n|no|否)$') {
                throw "用户取消准备 Python，未修改电脑环境。"
            }
        }

        $basePython = Install-ManagedPython `
            -PythonHome $managedPythonHome `
            -RuntimeDirectory $runtimeDirectory
        $basePythonArguments = @()
        $detectedVersion = Get-CompatiblePythonVersion -Path $basePython
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
            --no-cache-dir `
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
                --no-cache-dir `
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
            Download-VerifiedFile `
                -Label "下载 $($modelDefinition.Label)模型结构" `
                -Url "$modelBaseUrl/$modelName.xml" `
                -Destination $modelXml `
                -MinimumBytes $modelDefinition.XmlMinimumBytes `
                -ExpectedSha256 $modelDefinition.XmlSha256
            Download-VerifiedFile `
                -Label "下载 $($modelDefinition.Label)模型权重" `
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
