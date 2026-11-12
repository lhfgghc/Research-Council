[CmdletBinding()]
param(
    [ValidateRange(1, 65535)]
    [int]$Port = 8000,

    [switch]$SkipBuild
)

$ErrorActionPreference = "Stop"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$Python = Join-Path $Root ".venv\Scripts\python.exe"
$Frontend = Join-Path $Root "frontend"
$Index = Join-Path $Frontend "dist\index.html"
$BindAddress = "127.0.0.1"

if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) {
    throw "Python environment not found. Run scripts\setup.ps1 first."
}

$Listener = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Loopback, $Port)
$Listener.ExclusiveAddressUse = $true
try {
    $Listener.Start()
} catch [System.Net.Sockets.SocketException] {
    throw "Cannot listen on ${BindAddress}:${Port}. Stop the existing listener or choose another port with -Port. $($_.Exception.Message)"
} finally {
    $Listener.Stop()
}

if (-not $SkipBuild) {
    $Npm = Get-Command npm.cmd -CommandType Application -ErrorAction SilentlyContinue
    if (-not $Npm) {
        throw "npm.cmd not found. Install Node.js or use -SkipBuild with an existing frontend build."
    }

    Push-Location -LiteralPath $Frontend
    try {
        if (-not (Test-Path -LiteralPath "node_modules\.bin\vite.cmd" -PathType Leaf)) {
            Write-Host "Installing frontend dependencies..."
            & $Npm.Source ci
            if ($LASTEXITCODE -ne 0) {
                throw "npm ci failed with exit code $LASTEXITCODE."
            }
        }

        Write-Host "Building frontend..."
        & $Npm.Source run build
        if ($LASTEXITCODE -ne 0) {
            throw "Frontend build failed with exit code $LASTEXITCODE."
        }
    } finally {
        Pop-Location
    }
}

if (-not (Test-Path -LiteralPath $Index -PathType Leaf)) {
    throw "Frontend build not found. Run scripts\start.ps1 without -SkipBuild."
}

Push-Location -LiteralPath $Root
try {
    Write-Host "Starting Research Council at http://${BindAddress}:${Port} (Ctrl+C to stop)."
    & $Python -m uvicorn backend.app.main:app --host $BindAddress --port $Port
    if ($LASTEXITCODE -ne 0) {
        throw "Uvicorn exited with code $LASTEXITCODE."
    }
} finally {
    Pop-Location
}
