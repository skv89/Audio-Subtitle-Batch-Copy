param(
    [string]$ReleaseName = "Audio_and_Subtitle_Batch_Copy_v1.2.4_Windows_x64"
)

$ErrorActionPreference = "Stop"
$projectRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
$python = Join-Path $projectRoot ".venv\Scripts\python.exe"
$entryPoint = Join-Path $projectRoot "run_app.py"
$icon = Join-Path $projectRoot "assets\app_icon.ico"
$ffmpeg = Join-Path $projectRoot "tools\ffmpeg\bin\ffmpeg.exe"
$ffprobe = Join-Path $projectRoot "tools\ffmpeg\bin\ffprobe.exe"

foreach ($required in @($python, $entryPoint, $icon, $ffmpeg, $ffprobe)) {
    if (-not (Test-Path -LiteralPath $required -PathType Leaf)) {
        throw "Required build input is missing: $required"
    }
}

$stamp = Get-Date -Format "yyyyMMdd_HHmmss"
$workRoot = Join-Path $projectRoot "release_work\$stamp"
$distRoot = Join-Path $workRoot "dist"
$buildRoot = Join-Path $workRoot "build"
$specRoot = Join-Path $workRoot "spec"
$releaseRoot = Join-Path $projectRoot "dist_release"
$releaseDirectory = Join-Path $releaseRoot $ReleaseName

if (Test-Path -LiteralPath $releaseDirectory) {
    throw "Refusing to overwrite existing release directory: $releaseDirectory"
}

New-Item -ItemType Directory -Force -Path $distRoot, $buildRoot, $specRoot, $releaseRoot | Out-Null

& $python -m PyInstaller `
    --noconfirm `
    --onedir `
    --windowed `
    --noupx `
    --contents-directory "." `
    --name "Audio and Subtitle Batch Copy" `
    --icon $icon `
    --paths (Join-Path $projectRoot "src") `
    --add-data "$(Join-Path $projectRoot 'assets');assets" `
    --add-data "$(Join-Path $projectRoot 'tools');tools" `
    --distpath $distRoot `
    --workpath $buildRoot `
    --specpath $specRoot `
    $entryPoint
if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller failed with exit code $LASTEXITCODE"
}

$builtDirectory = Join-Path $distRoot "Audio and Subtitle Batch Copy"
if (-not (Test-Path -LiteralPath $builtDirectory -PathType Container)) {
    throw "PyInstaller output directory was not created: $builtDirectory"
}

Copy-Item -LiteralPath $builtDirectory -Destination $releaseDirectory -Recurse
Copy-Item -LiteralPath (Join-Path $projectRoot "README.md") -Destination $releaseDirectory
Copy-Item -LiteralPath (Join-Path $projectRoot "LICENSE.txt") -Destination $releaseDirectory
Copy-Item -LiteralPath (Join-Path $projectRoot "THIRD_PARTY_NOTICES.txt") -Destination $releaseDirectory

$licenseDirectory = Join-Path $releaseDirectory "licenses"
New-Item -ItemType Directory -Force -Path $licenseDirectory | Out-Null

$pythonBase = (& $python -c "import sys; print(sys.base_prefix)").Trim()
$pythonLicense = Join-Path $pythonBase "LICENSE.txt"
if (-not (Test-Path -LiteralPath $pythonLicense -PathType Leaf)) {
    throw "Python license file was not found under the active interpreter: $pythonLicense"
}

$sitePackages = (& $python -c "import sysconfig; print(sysconfig.get_paths()['purelib'])").Trim()
$pyinstallerLicense = Get-ChildItem -LiteralPath $sitePackages -Directory -Filter "pyinstaller-*.dist-info" |
    ForEach-Object { Join-Path $_.FullName "licenses\COPYING.txt" } |
    Where-Object { Test-Path -LiteralPath $_ -PathType Leaf } |
    Select-Object -First 1
if (-not $pyinstallerLicense) {
    throw "PyInstaller license file was not found in the active environment."
}

Copy-Item -LiteralPath $pythonLicense -Destination (Join-Path $licenseDirectory "PYTHON_LICENSE.txt")
Copy-Item -LiteralPath $pyinstallerLicense -Destination (Join-Path $licenseDirectory "PYINSTALLER_LICENSE.txt")
Copy-Item -LiteralPath (Join-Path $projectRoot "licenses\QT_LGPL_3_0.txt") -Destination $licenseDirectory

Write-Output $releaseDirectory
