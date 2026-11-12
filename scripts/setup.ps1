$ErrorActionPreference = "Stop"
$Root = Resolve-Path (Join-Path $PSScriptRoot "..")
Set-Location $Root

Write-Host "Setting up Research Council in $Root"

if (-not (Test-Path ".venv\Scripts\python.exe")) {
    python -m venv .venv
}

& .venv\Scripts\python.exe -m pip install --upgrade pip
& .venv\Scripts\python.exe -m pip install --index-url https://pypi.org/simple --trusted-host pypi.org --trusted-host files.pythonhosted.org -r backend\requirements.txt

if (-not (Test-Path ".env")) {
    Copy-Item .env.example .env
    Write-Host "Created .env from .env.example. Configure each model in the workbench after starting."
}

Push-Location frontend
npm install
npm run build
Pop-Location

Write-Host "Setup complete. Run scripts\start.ps1 to launch."
