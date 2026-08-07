$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoRoot = Resolve-Path (Join-Path $ScriptDir "..")
Set-Location $RepoRoot

python -m pip install -r requirements.txt

python -m PyInstaller `
  --noconfirm `
  --clean `
  --windowed `
  --name FundValuation `
  --add-data "web;web" `
  --hidden-import app `
  --hidden-import fundval.providers `
  --hidden-import fundval.service `
  --hidden-import fundval.store `
  --hidden-import fundval.valuation `
  --collect-all akshare `
  --collect-all pandas `
  desktop/main.py

Write-Host "Windows desktop build created under dist\FundValuation"
