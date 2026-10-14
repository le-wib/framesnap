# Build FrameSnap for Windows: a portable zip and, when Inno Setup can be
# installed, a regular installer (same script locally and in CI).
#
#   pip install -r requirements-dev.txt
#   powershell -ExecutionPolicy Bypass -File packaging\build_windows.ps1
#
# ffmpeg comes from the imageio-ffmpeg wheel (static GPL build with x264).
$ErrorActionPreference = "Stop"
Set-Location (Split-Path -Parent $PSScriptRoot)

$version = (python -c "import framesnap; print(framesnap.__version__)").Trim()
$ffmpegSrc = (python -c "import imageio_ffmpeg; print(imageio_ffmpeg.get_ffmpeg_exe())").Trim()

Remove-Item -Recurse -Force build, dist -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force build\bin | Out-Null
Copy-Item $ffmpegSrc build\bin\ffmpeg.exe
& build\bin\ffmpeg.exe -hide_banner -version | Select-Object -First 1

# rife-ncnn-vulkan (MIT): runs on the graphics card through its Vulkan driver (NVIDIA, AMD, Intel)
python packaging\fetch_rife.py windows --dest build\rife
if ($LASTEXITCODE -ne 0) { throw "fetch_rife failed" }

function Build-App([string]$mode) {
    Remove-Item -Recurse -Force dist, build\FrameSnap -ErrorAction SilentlyContinue
    $piArgs = @(
        "--noconfirm", "--clean", "--windowed", "--name", "FrameSnap",
        "--icon", "packaging\icon.ico",
        "--add-data", "framesnap\ui;framesnap\ui",
        "--add-data", "build\rife;rife",
        "--add-binary", "build\bin\ffmpeg.exe;bin",
        "--collect-submodules", "webview",
        "--collect-all", "cv2",
        "--exclude-module", "imageio_ffmpeg"
    )
    if ($mode -eq "full") {
        $piArgs += @("--collect-all", "webview", "--collect-all", "pythonnet", "--collect-all", "clr_loader")
    }
    $piArgs += "run_app.py"
    & pyinstaller @piArgs
    if ($LASTEXITCODE -ne 0) { throw "pyinstaller failed (mode $mode)" }
}

function Test-App {
    $log = Join-Path $PWD "build\selftest.log"
    Remove-Item $log -ErrorAction SilentlyContinue
    $env:FRAMESNAP_SELFTEST_LOG = $log
    $exe = Join-Path $PWD "dist\FrameSnap\FrameSnap.exe"
    $p = Start-Process -FilePath $exe -ArgumentList "--selftest" -Wait -PassThru
    if (Test-Path $log) { Get-Content $log | Write-Host }
    return ($p.ExitCode -eq 0)
}

$ok = $false
foreach ($mode in @("standard", "full")) {
    Write-Host "=== building (mode: $mode)"
    Build-App $mode
    Get-ChildItem -Recurse dist -Filter ffmpeg.exe | ForEach-Object { Write-Host $_.FullName }
    if (Test-App) { $ok = $true; break }
    Write-Host "selftest failed in mode $mode"
}
if (-not $ok) { throw "the packaged app failed its selftest" }

# Portable zip
$zip = "dist\FrameSnap-$version-Windows.zip"
Compress-Archive -Path dist\FrameSnap -DestinationPath $zip -Force
Write-Host "built: $zip"

# Installer (optional: a failure here never blocks the zip)
function Find-Iscc {
    $paths = @(
        "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
        "${env:ProgramFiles}\Inno Setup 6\ISCC.exe",
        "${env:LOCALAPPDATA}\Programs\Inno Setup 6\ISCC.exe"
    )
    foreach ($p in $paths) { if (Test-Path $p) { return $p } }
    return $null
}
try {
    $iscc = Find-Iscc
    if (-not $iscc) {
        Write-Host "installing Inno Setup"
        choco install innosetup -y --no-progress
        $iscc = Find-Iscc
    }
    if (-not $iscc) { throw "Inno Setup not found" }
    & $iscc "/DAppVersion=$version" "packaging\windows\installer.iss"
    if ($LASTEXITCODE -ne 0) { throw "ISCC failed" }
    Write-Host "built installer"
} catch {
    Write-Host "WARNING: installer not built, the portable zip is still available. $_"
}
Get-ChildItem dist -File | ForEach-Object { Write-Host $_.Name }
