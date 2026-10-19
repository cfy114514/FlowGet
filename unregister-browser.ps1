$ErrorActionPreference = 'Stop'
# Remove only this app's own registrations. Leave user downloads and app data.
foreach ($key in @('HKCU:\Software\Microsoft\Edge\NativeMessagingHosts\com.flowget.bridge', 'HKCU:\Software\Google\Chrome\NativeMessagingHosts\com.flowget.bridge')) {
    if (Test-Path -LiteralPath $key) { Remove-Item -LiteralPath $key }
}
Write-Host 'FlowGet native host registrations removed. Remove the browser extension in the browser settings separately.'
