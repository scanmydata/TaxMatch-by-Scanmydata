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

# 3b. version_info.txt (Windows FileVersion/ProductVersion) από το ίδιο __version__ — ΠΟΤΕ στο χέρι: ξέμεινε
# hardcoded στο 0.3.1.0 ενώ το __version__ ήταν ήδη 0.3.2 (2026-09-23), το installer.exe έδειχνε λάθος αριθμό.
$parts = ($version -split '\.') + @('0', '0', '0', '0') | Select-Object -First 4
$verTuple = $parts -join ', '
$verDots = $parts -join '.'
@"
# UTF-8
VSVersionInfo(
  ffi=FixedFileInfo(filevers=($verTuple), prodvers=($verTuple), mask=0x3f, flags=0x0, OS=0x40004, fileType=0x1, subtype=0x0, date=(0, 0)),
  kids=[
    StringFileInfo([StringTable('040804b0', [
      StringStruct('CompanyName', 'ScanMyData'),
      StringStruct('FileDescription', 'TaxMatch by ScanMyData'),
      StringStruct('FileVersion', '$verDots'),
      StringStruct('InternalName', 'TaxMatch'),
      StringStruct('OriginalFilename', 'TaxMatch.exe'),
      StringStruct('ProductName', 'TaxMatch by ScanMyData'),
      StringStruct('ProductVersion', '$verDots')])]),
    VarFileInfo([VarStruct('Translation', [0x0408, 1200])])
  ]
)
"@ | Set-Content -LiteralPath "packaging\version_info.txt" -Encoding utf8

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
