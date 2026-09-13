$ErrorActionPreference = "Stop"

$pythonCandidates = @(
    $env:RAVENLIB_PYTHON,
    (Get-Command python -ErrorAction SilentlyContinue).Source,
    (Get-Command py -ErrorAction SilentlyContinue).Source
)
$python = $null
foreach ($candidate in $pythonCandidates) {
    if (-not ($candidate -and (Test-Path -LiteralPath $candidate))) {
        continue
    }

    & $candidate -c "import pywebview, PyInstaller" 2>$null
    if ($LASTEXITCODE -eq 0) {
        $python = $candidate
        break
    }
}

if (-not $python) {
    throw "Python with pywebview and PyInstaller was not found. Run: python -m pip install pywebview pyinstaller"
}

Push-Location $PSScriptRoot
try {
    $distPath = Join-Path $PSScriptRoot "dist"
    $workPath = Join-Path ([IO.Path]::GetTempPath()) "RavenLibClient-pyinstaller"
    & $python -m PyInstaller .\RavenLibClient.spec --distpath $distPath --workpath $workPath --clean --noconfirm
    if ($LASTEXITCODE -ne 0) {
        throw "PyInstaller failed with exit code $LASTEXITCODE."
    }

    Write-Host "Build complete: $distPath\RavenLibClient.exe"
}
finally {
    Pop-Location
}
