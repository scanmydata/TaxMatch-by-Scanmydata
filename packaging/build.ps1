<#
  Χτίζει τον installer:  powershell -ExecutionPolicy Bypass -File packaging\build.ps1 [-SkipTests]
  Βήματα: venv + εξαρτήσεις -> tests -> εικονίδια -> PyInstaller (dist\TaxMatch) -> Inno Setup (installer-output\).
  Απαιτεί: Python 3.12 (ή uv) και Inno Setup 6 (ISCC.exe).
#>
param([switch]$SkipTests)
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

$version = (Select-String -Path "taxmatch\__init__.py" -Pattern '__version__ = "([^"]+)"').Matches[0].Groups[1].Value
Write-Host "TaxMatch $version" -ForegroundColor Cyan

# 1. venv (Python 3.12: το ίδιο σε όλο το έργο — βλ. CLAUDE.md)
$py = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) {
  if (Get-Command uv -ErrorAction SilentlyContinue) { uv venv --python 3.12 .venv }
  else { py -3.12 -m venv .venv }
}
if (Get-Command uv -ErrorAction SilentlyContinue) { uv pip install --python $py -r requirements-dev.txt }
else { & $py -m pip install -r requirements-dev.txt }

# 2. tests
if (-not $SkipTests) { & $py -m pytest; if ($LASTEXITCODE -ne 0) { throw "Τα tests απέτυχαν" } }

# 3. εικονίδια από το λογότυπο
& $py packaging\make_icons.py

# 4. PyInstaller
if (Test-Path dist) { Remove-Item -LiteralPath dist -Recurse -Force }
if (Test-Path build) { Remove-Item -LiteralPath build -Recurse -Force }
& $py -m PyInstaller packaging\taxmatch.spec --noconfirm --distpath dist --workpath build
if ($LASTEXITCODE -ne 0) { throw "PyInstaller απέτυχε" }

# 5. Inno Setup
$iscc = @("$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe", "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe", "$env:ProgramFiles\Inno Setup 6\ISCC.exe") |
  Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $iscc) { throw "Δεν βρέθηκε ο Inno Setup 6 (ISCC.exe)" }
& $iscc packaging\installer.iss "/DAppVersion=$version"
if ($LASTEXITCODE -ne 0) { throw "Inno Setup απέτυχε" }

Write-Host "Έτοιμο: installer-output\TaxMatch-Setup-$version.exe" -ForegroundColor Green
