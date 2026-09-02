$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
[Environment]::CurrentDirectory = (Get-Location).Path

$venv = Join-Path $PSScriptRoot ".venv\Scripts"
if (Test-Path $venv) { $env:Path = "$venv;$env:Path" }

ruff format --check src tests
if ($LASTEXITCODE -ne 0) { throw "formatting drift: run  ruff format src tests" }
ruff check src tests
if ($LASTEXITCODE -ne 0) { throw "ruff check failed" }
mypy
if ($LASTEXITCODE -ne 0) { throw "mypy failed" }
pytest -q
if ($LASTEXITCODE -ne 0) { throw "pytest failed" }
mdformat --check --wrap no README.md DESIGN.md
if ($LASTEXITCODE -ne 0) { throw "markdown drift: run  mdformat --wrap no README.md DESIGN.md" }
if (Select-String -Path README.md,DESIGN.md -Pattern "&#x20;|\\[#\[\*_]" -Quiet) {
  throw "escaped markdown punctuation in docs"
}
Write-Host "`nall checks passed" -ForegroundColor Green