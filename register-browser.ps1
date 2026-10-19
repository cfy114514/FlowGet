param(
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^[a-p]{32}$')]
    [string]$ExtensionId,
    [ValidateSet('Edge', 'Chrome', 'Both')]
    [string]$Browser = 'Edge'
)
$ErrorActionPreference = 'Stop'
$bridgeExe = Join-Path $PSScriptRoot 'FlowGetBridge.exe'
if (-not (Test-Path -LiteralPath $bridgeExe)) { throw 'FlowGetBridge.exe is missing; build the bridge first.' }
$bridgeData = Join-Path $env:LOCALAPPDATA 'FlowGet'
New-Item -ItemType Directory -Path $bridgeData -Force | Out-Null
$configPath = Join-Path $bridgeData 'bridge.json'
$origin = "chrome-extension://$ExtensionId/"
$allowed = @()
if (Test-Path -LiteralPath $configPath) {
    $allowed = @((Get-Content -LiteralPath $configPath -Raw | ConvertFrom-Json).allowed_origins)
}
$allowed = @($allowed + $origin | Sort-Object -Unique)
$encoding = New-Object System.Text.UTF8Encoding($false)
[System.IO.File]::WriteAllText($configPath, (@{ allowed_origins = $allowed } | ConvertTo-Json -Depth 4), $encoding)
$manifestPath = Join-Path $bridgeData 'com.flowget.bridge.json'
$manifest = @{
    name = 'com.flowget.bridge'
    description = 'FlowGet explicit-link bridge'
    path = $bridgeExe
    type = 'stdio'
    allowed_origins = $allowed
}
[System.IO.File]::WriteAllText($manifestPath, ($manifest | ConvertTo-Json -Depth 4), $encoding)
$browsers = if ($Browser -eq 'Both') { @('Edge', 'Chrome') } else { @($Browser) }
foreach ($targetBrowser in $browsers) {
    $base = if ($targetBrowser -eq 'Edge') { 'HKCU:\Software\Microsoft\Edge\NativeMessagingHosts' } else { 'HKCU:\Software\Google\Chrome\NativeMessagingHosts' }
    $key = Join-Path $base 'com.flowget.bridge'
    New-Item -Path $key -Force | Out-Null
    Set-Item -LiteralPath $key -Value $manifestPath
    Write-Host "Registered $targetBrowser for extension $ExtensionId"
}
Write-Host 'Only the selected extension can use this host. Keep the executables at their current location.'
