$ErrorActionPreference = 'Stop'
Push-Location -LiteralPath $PSScriptRoot
try {
    python -m pip install pyinstaller==6.16.0 pyinstaller-hooks-contrib==2025.8
    if ($LASTEXITCODE -ne 0) { throw 'Build dependency installation failed' }
    python -m PyInstaller --noconfirm --onefile --windowed --name FlowGet --manifest (Join-Path $PSScriptRoot 'FlowGet.manifest') --distpath $PSScriptRoot --workpath (Join-Path $PSScriptRoot '.build') --specpath (Join-Path $PSScriptRoot '.build') (Join-Path $PSScriptRoot 'app.py')
    if ($LASTEXITCODE -ne 0) { throw 'Build failed' }
    python -m PyInstaller --noconfirm --onefile --console --name FlowGetBridge --distpath $PSScriptRoot --workpath (Join-Path $PSScriptRoot '.build-bridge') --specpath (Join-Path $PSScriptRoot '.build-bridge') (Join-Path $PSScriptRoot 'native_host.py')
    if ($LASTEXITCODE -ne 0) { throw 'Bridge build failed' }
} finally {
    Pop-Location
}
