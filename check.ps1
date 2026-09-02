# Local equivalent of `make check` (CI runs the Makefile on Linux).
$ErrorActionPreference = "Stop"
ruff format src tests
ruff check src tests
if ($LASTEXITCODE -ne 0) { throw "ruff check failed" }
mypy
if ($LASTEXITCODE -ne 0) { throw "mypy failed" }
pytest -q
if ($LASTEXITCODE -ne 0) { throw "pytest failed" }
Write-Host "`nall checks passed" -ForegroundColor Green