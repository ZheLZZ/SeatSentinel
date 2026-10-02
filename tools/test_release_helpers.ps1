[CmdletBinding()]
param([string]$SourceRoot = '')
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
if (-not $SourceRoot) { $SourceRoot = Split-Path -Parent $PSScriptRoot }
. (Join-Path $SourceRoot 'tools\release_helpers.ps1')

function Assert-Test {
    param([bool]$Condition, [string]$Message)
    if (-not $Condition) { throw $Message }
}

$scripts = @((Get-ChildItem -LiteralPath $SourceRoot -Filter '*.ps1' -File)) +
           @((Get-ChildItem -LiteralPath (Join-Path $SourceRoot 'tools') -Filter '*.ps1' -File))
foreach ($scriptFile in $scripts) {
    $tokens = $null
    $parseErrors = $null
    [void][Management.Automation.Language.Parser]::ParseFile($scriptFile.FullName, [ref]$tokens, [ref]$parseErrors)
    Assert-Test -Condition ($parseErrors.Count -eq 0) -Message ("PowerShell parse failed: " + $scriptFile.Name)
}

$tempParent = [IO.Path]::GetTempPath().TrimEnd('\', '/')
$fixture = New-ReleaseStage -OutputRoot $tempParent
try {
    $output = Join-Path $fixture 'output'
    [void](New-Item -ItemType Directory -Path $output)
    $escaped = $false
    try { [void](Assert-ReleasePath -OutputRoot $output -Path (Join-Path $fixture 'outside')) }
    catch { $escaped = $true }
    Assert-Test $escaped 'Path containment allowed an outside target.'

    # A successful publication keeps its predecessor. A later multi-artifact
    # failure must roll every completed move back, without deleting either copy.
    $stage = New-ReleaseStage -OutputRoot $output
    $old = Join-Path $output 'one.txt'
    $new = Join-Path $stage 'one.txt'
    Set-Content -LiteralPath $old -Value 'old-one' -Encoding ASCII
    Set-Content -LiteralPath $new -Value 'new-one' -Encoding ASCII
    Publish-ReleaseArtifacts -OutputRoot $output -Items @([PSCustomObject]@{Source=$new; Name='one.txt'})
    Assert-Test ((Get-Content -LiteralPath $old -Raw).Trim() -eq 'new-one') 'Publication did not install the new artifact.'
    $backups = @(Get-ChildItem -LiteralPath (Join-Path $output 'backups') -Recurse -File)
    Assert-Test ($backups.Count -eq 1) 'Publication did not preserve the old artifact.'
    Assert-Test ((Get-Content -LiteralPath $backups[0].FullName -Raw).Trim() -eq 'old-one') 'Backup content changed.'

    $newTwo = Join-Path $stage 'two.txt'
    $oldTwo = Join-Path $output 'two.txt'
    Set-Content -LiteralPath $new -Value 'third-one' -Encoding ASCII
    Set-Content -LiteralPath $newTwo -Value 'new-two' -Encoding ASCII
    Set-Content -LiteralPath $oldTwo -Value 'old-two' -Encoding ASCII
    $script:failSource = $newTwo
    function Move-Item {
        param([string]$LiteralPath, [string]$Destination)
        if ($LiteralPath -eq $script:failSource) { throw 'Synthetic second-artifact move failure.' }
        Microsoft.PowerShell.Management\Move-Item -LiteralPath $LiteralPath -Destination $Destination
    }
    $failed = $false
    try {
        Publish-ReleaseArtifacts -OutputRoot $output -Items @(
            [PSCustomObject]@{Source=$new; Name='one.txt'},
            [PSCustomObject]@{Source=$newTwo; Name='two.txt'}
        )
    }
    catch { $failed = $true }
    finally { Remove-Item -LiteralPath 'Function:\Move-Item' }
    Assert-Test $failed 'Publication failure injection was not reached.'
    Assert-Test ((Get-Content -LiteralPath $old -Raw).Trim() -eq 'new-one') 'Rollback lost the first old artifact.'
    Assert-Test ((Get-Content -LiteralPath $oldTwo -Raw).Trim() -eq 'old-two') 'Rollback lost the second old artifact.'
    Assert-Test ((Test-Path -LiteralPath $new) -and (Test-Path -LiteralPath $newTwo)) 'Rollback lost a staged artifact.'

    $allowlistSource = Join-Path $fixture 'allowlist'
    [void](New-Item -ItemType Directory -Path $allowlistSource)
    Set-Content -LiteralPath (Join-Path $allowlistSource 'public.txt') -Value 'public fixture' -Encoding ASCII
    Set-Content -LiteralPath (Join-Path $allowlistSource 'untracked-private.py') -Value '# excluded fixture' -Encoding ASCII
    Set-Content -LiteralPath (Join-Path $allowlistSource 'release-manifest.json') -Encoding ASCII -Value (
        '{"schema_version":1,"light_files":["public.txt"],"full_documents":["public.txt"],"full_assets":["public.txt"]}')
    $allowlist = @(Get-ReleaseFiles -SourceRoot $allowlistSource -Section light_files)
    $package = Join-Path $fixture 'Package'
    Copy-ReleaseFiles -SourceRoot $allowlistSource -DestinationRoot $package -Files $allowlist
    Assert-Test (-not (Test-Path -LiteralPath (Join-Path $package 'untracked-private.py'))) 'Unlisted file entered the release.'
    $archive = Join-Path $fixture 'fixture.zip'
    Compress-Archive -LiteralPath $package -DestinationPath $archive
    Assert-ReleaseArchive -Archive $archive -PackageName 'Package' -ExpectedFiles $allowlist

    # With no models, verification must exit with an error before looking for
    # Python, creating a venv, installing dependencies, or downloading anything.
    $verifyRoot = Join-Path $fixture 'verify-only'
    Copy-ReleaseFiles -SourceRoot $SourceRoot -DestinationRoot $verifyRoot -Files @(
        '一键启动.ps1', 'model-manifest.json', 'tools/release_helpers.ps1')
    $verifyOutput = & powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File (
        Join-Path $verifyRoot '一键启动.ps1') -VerifyModelsOnly 2>&1
    Assert-Test ($LASTEXITCODE -eq 1) 'Missing models did not fail verify-only.'
    Assert-Test (([string]($verifyOutput -join "`n")).Contains('Missing or invalid model SHA-256')) 'Verify-only took an unexpected path.'
    Assert-Test (-not (Test-Path -LiteralPath (Join-Path $verifyRoot '.venv'))) 'Verify-only created a venv.'
    Assert-Test (-not (Test-Path -LiteralPath (Join-Path $verifyRoot 'models'))) 'Verify-only created model files.'
    Write-Host 'PASS: PowerShell syntax, path containment, preserved backups, rollback, ZIP allowlist, read-only model verification.'
}
finally {
    Remove-ReleaseStage -OutputRoot $tempParent -Stage $fixture
}
