# Downloads a static Windows FFmpeg build into vendor\ffmpeg (ffmpeg.exe + ffprobe.exe).
# Used by the GitHub Actions build and by run_dev.bat.
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"

$root = Split-Path -Parent $PSScriptRoot
$dest = Join-Path $root "vendor\ffmpeg"
New-Item -ItemType Directory -Force -Path $dest | Out-Null
if ((Test-Path (Join-Path $dest "ffmpeg.exe")) -and (Test-Path (Join-Path $dest "ffprobe.exe"))) {
    Write-Host "FFmpeg already present in $dest"
    exit 0
}

$tmpBase = if ($env:RUNNER_TEMP) { $env:RUNNER_TEMP } else { $env:TEMP }
$zip = Join-Path $tmpBase "lk-ffmpeg.zip"
$work = Join-Path $tmpBase "lk-ffmpeg"
$urls = @(
    "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip",
    "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-win64-gpl.zip"
)
$got = $false
foreach ($u in $urls) {
    try {
        Write-Host "Downloading $u"
        Invoke-WebRequest -Uri $u -OutFile $zip -UseBasicParsing
        $got = $true
        break
    } catch {
        Write-Warning "Download failed from $u : $_"
    }
}
if (-not $got) { throw "Could not download FFmpeg from any mirror." }

if (Test-Path $work) { Remove-Item $work -Recurse -Force }
Expand-Archive -Path $zip -DestinationPath $work -Force
foreach ($name in @("ffmpeg.exe", "ffprobe.exe")) {
    $f = Get-ChildItem $work -Recurse -Filter $name | Select-Object -First 1
    if (-not $f) { throw "$name not found in archive" }
    Copy-Item $f.FullName -Destination $dest -Force
}
$lic = Get-ChildItem $work -Recurse -Include "LICENSE*", "COPYING*" | Select-Object -First 1
if ($lic) { Copy-Item $lic.FullName -Destination (Join-Path $dest "FFMPEG-LICENSE.txt") -Force }
Remove-Item $zip -Force
Remove-Item $work -Recurse -Force
& (Join-Path $dest "ffmpeg.exe") -hide_banner -version | Select-Object -First 1
