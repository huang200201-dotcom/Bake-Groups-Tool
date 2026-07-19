[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^[0-9]+\.[0-9]+\.[0-9]+(?:[-+][0-9A-Za-z.-]+)?$')]
    [string]$Version,

    [Parameter(Mandatory = $true)]
    [ValidatePattern('^[0-9]+\.[0-9]+\.[0-9]+$')]
    [string]$RuntimeVersion,

    [Parameter(Mandatory = $true)]
    [ValidatePattern('^[A-Za-z0-9_.-]+$')]
    [string]$Owner,

    [Parameter(Mandatory = $true)]
    [ValidatePattern('^[A-Za-z0-9_.-]+$')]
    [string]$Repository,

    [string]$Tag,
    [ValidateSet('stable', 'beta')]
    [string]$Channel = 'stable',
    [string]$OutputDirectory
)

Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'
$utf8NoBom = New-Object System.Text.UTF8Encoding($false)
$repoRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))

if (-not $Tag) {
    $Tag = "v$Version"
}
if ($Tag -notmatch '^v[0-9]+\.[0-9]+\.[0-9]+(?:[-+][0-9A-Za-z.-]+)?$') {
    throw "Invalid release tag: $Tag"
}
if (-not $OutputDirectory) {
    $OutputDirectory = Join-Path $repoRoot 'dist'
}
$OutputDirectory = [System.IO.Path]::GetFullPath($OutputDirectory)

$pluginSource = Join-Path $repoRoot 'plugin\Bake_Groups'
$installerSource = Join-Path $repoRoot 'installer'
$runtimeSource = Join-Path $pluginSource ("versions\{0}" -f $RuntimeVersion)
$requiredSourceFiles = @(
    (Join-Path $pluginSource 'launcher.py'),
    (Join-Path $pluginSource 'active_version.json'),
    (Join-Path $pluginSource 'update_config.json'),
    (Join-Path $pluginSource 'license_config.json'),
    (Join-Path $runtimeSource 'bg_main_window.py')
)
$requiredSourceFiles += @(
    (Join-Path $runtimeSource 'bg_update.py'),
    (Join-Path $runtimeSource 'bg_credentials.py'),
    (Join-Path $runtimeSource 'bg_license.py')
)
foreach ($mayaVersion in @('2022', '2023', '2024', '2025', '2026', '2027')) {
    $requiredSourceFiles += Join-Path $runtimeSource ("bin\{0}\bg_math_core.pyd" -f $mayaVersion)
}
foreach ($path in $requiredSourceFiles) {
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
        throw "Required release file is missing: $path"
    }
}
if (-not (Test-Path -LiteralPath $installerSource -PathType Container)) {
    throw "Installer folder is missing: $installerSource"
}

New-Item -ItemType Directory -Path $OutputDirectory -Force | Out-Null
$assetBase = "Bake_Groups_Tool_{0}_Windows_x64" -f $Version
$stageRoot = Join-Path $OutputDirectory (".stage-{0}" -f [Guid]::NewGuid().ToString('N'))
$packageRoot = Join-Path $stageRoot $assetBase
$zipPath = Join-Path $OutputDirectory ("{0}.zip" -f $assetBase)
$checksumPath = "$zipPath.sha256"
$stableOutputPath = Join-Path $OutputDirectory 'stable.json'

try {
    New-Item -ItemType Directory -Path $packageRoot -Force | Out-Null
    Copy-Item -LiteralPath $pluginSource -Destination (Join-Path $packageRoot 'Bake_Groups') -Recurse -Force

    # An update archive must expose exactly one runtime payload. Keep older
    # versions in source control for rollback, but never ship them together.
    $packagedVersions = Join-Path $packageRoot 'Bake_Groups\versions'
    Get-ChildItem -LiteralPath $packagedVersions -Directory -Force | Where-Object {
        $_.Name -ne $RuntimeVersion
    } | Remove-Item -Recurse -Force

    Get-ChildItem -LiteralPath $installerSource -File -Force | Where-Object {
        $_.Name -ne 'SHA256.txt'
    } | ForEach-Object {
        Copy-Item -LiteralPath $_.FullName -Destination (Join-Path $packageRoot $_.Name) -Force
    }

    Get-ChildItem -LiteralPath $packageRoot -Directory -Recurse -Force | Where-Object {
        $_.Name -eq '__pycache__'
    } | Remove-Item -Recurse -Force
    Get-ChildItem -LiteralPath $packageRoot -File -Recurse -Force | Where-Object {
        $_.Extension -eq '.pyc' -or $_.Name -eq 'desktop.ini'
    } | Remove-Item -Force

    $activeVersion = [ordered]@{
        active_version = $RuntimeVersion
        package_version = $Version
    }
    [System.IO.File]::WriteAllText(
        (Join-Path $packageRoot 'Bake_Groups\active_version.json'),
        ($activeVersion | ConvertTo-Json),
        $utf8NoBom
    )

    $packageInfo = [ordered]@{
        name = 'Bake Master'
        version = $Version
        runtime = $RuntimeVersion
        channel = $Channel
        platform = 'Windows x64'
        maya_versions = @('2022', '2023', '2024', '2025', '2026', '2027')
        network_source = 'public-signed-github-release'
    }
    [System.IO.File]::WriteAllText(
        (Join-Path $packageRoot 'PACKAGE_INFO.json'),
        ($packageInfo | ConvertTo-Json -Depth 5),
        $utf8NoBom
    )

    $installReadme = Join-Path $packageRoot '安装说明.txt'
    if (Test-Path -LiteralPath $installReadme -PathType Leaf) {
        $readmeLines = [System.IO.File]::ReadAllLines($installReadme)
        if ($readmeLines.Count -gt 0) {
            $readmeLines[0] = "Bake Master $Version"
            [System.IO.File]::WriteAllLines($installReadme, $readmeLines, $utf8NoBom)
        }
    }

    $payloadRoot = Join-Path $packageRoot 'Bake_Groups'
    $payloadFiles = @(Get-ChildItem -LiteralPath $payloadRoot -File -Recurse | Sort-Object FullName | ForEach-Object {
        $relative = $_.FullName.Substring($packageRoot.Length).TrimStart('\').Replace('\', '/')
        [ordered]@{
            path = $relative
            sha256 = (Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash
            size = [int64]$_.Length
        }
    })
    $updatePackage = [ordered]@{
        schema_version = 1
        product = 'Bake Master'
        package_version = $Version
        runtime_version = $RuntimeVersion
        channel = $Channel
        platform = 'windows-x64'
        maya_versions = @('2022', '2023', '2024', '2025', '2026', '2027')
        payload = [ordered]@{
            root = 'Bake_Groups'
            entrypoint = 'launcher.py'
            active_version_file = 'active_version.json'
        }
        files = $payloadFiles
    }
    [System.IO.File]::WriteAllText(
        (Join-Path $packageRoot 'UPDATE_PACKAGE.json'),
        ($updatePackage | ConvertTo-Json -Depth 8),
        $utf8NoBom
    )

    $manifestPath = Join-Path $packageRoot 'SHA256.txt'
    $manifestLines = @(Get-ChildItem -LiteralPath $packageRoot -File -Recurse | Where-Object {
        $_.FullName -ne $manifestPath
    } | Sort-Object FullName | ForEach-Object {
        $relative = $_.FullName.Substring($packageRoot.Length).TrimStart('\')
        $hash = (Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash
        "$hash  $relative"
    })
    [System.IO.File]::WriteAllLines($manifestPath, $manifestLines, $utf8NoBom)

    if (Test-Path -LiteralPath $zipPath -PathType Leaf) {
        Remove-Item -LiteralPath $zipPath -Force
    }
    Compress-Archive -Path (Join-Path $packageRoot '*') -DestinationPath $zipPath -CompressionLevel Optimal
    $zipFile = Get-Item -LiteralPath $zipPath
    $zipHash = (Get-FileHash -LiteralPath $zipPath -Algorithm SHA256).Hash
    [System.IO.File]::WriteAllText(
        $checksumPath,
        ("{0}  {1}`r`n" -f $zipHash, $zipFile.Name),
        $utf8NoBom
    )

    $stable = [ordered]@{
        schema_version = 1
        channel = $Channel
        product = 'Bake Master'
        version = $Version
        runtime_version = $RuntimeVersion
        repository = "$Owner/$Repository"
        tag = $Tag
        release_api_url = "https://api.github.com/repos/$Owner/$Repository/releases/tags/$Tag"
        asset_name = $zipFile.Name
        asset_sha256 = $zipHash
        asset_size = [int64]$zipFile.Length
        package_manifest = 'UPDATE_PACKAGE.json'
        platform = 'windows-x64'
        maya_versions = @('2022', '2023', '2024', '2025', '2026', '2027')
        published_at = [DateTime]::UtcNow.ToString('yyyy-MM-ddTHH:mm:ssZ')
    }
    [System.IO.File]::WriteAllText(
        $stableOutputPath,
        ($stable | ConvertTo-Json -Depth 5),
        $utf8NoBom
    )

    Write-Output "Release ZIP: $zipPath"
    Write-Output "ZIP SHA256: $zipHash"
    Write-Output "Stable manifest: $stableOutputPath"
}
finally {
    if (Test-Path -LiteralPath $stageRoot) {
        Remove-Item -LiteralPath $stageRoot -Recurse -Force
    }
}
