# Show this PC's LAN IP(s) so you can set TRENDRADAR_DASHBOARD_URL in .env
# Usage:  powershell -ExecutionPolicy Bypass -File scripts\show_lan_ip.ps1

Write-Host ""
Write-Host "本机 IPv4 地址 (你 PC 在 LAN 上的 IP):" -ForegroundColor Cyan
Write-Host ""

$ips = Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
    Where-Object {
        $_.IPAddress -notlike '127.*' -and
        $_.IPAddress -notlike '169.254.*' -and
        $_.PrefixOrigin -ne 'WellKnown'
    } |
    Select-Object IPAddress, InterfaceAlias

if (-not $ips) {
    Write-Host "  没找到 LAN IP. 试一下:  ipconfig" -ForegroundColor Yellow
    exit
}

foreach ($ip in $ips) {
    $url = "http://$($ip.IPAddress):8001"
    Write-Host ("  {0,-18}  {1}" -f $ip.IPAddress, $ip.InterfaceAlias)
    Write-Host ("  → 仪表盘地址: {0}" -f $url) -ForegroundColor Green
    Write-Host ""
}

Write-Host "下一步: 把上面 ↑ 那个 URL 填到 .env 的 TRENDRADAR_DASHBOARD_URL=" -ForegroundColor Cyan
Write-Host "       手机连同一 WiFi 后, 在浏览器打开该 URL 即可访问。" -ForegroundColor Cyan
Write-Host ""
Write-Host "如果手机打不开, 检查:" -ForegroundColor Yellow
Write-Host "  1. python run.py 启动时是否在监听 0.0.0.0 (默认就是, 看启动 log)" -ForegroundColor Yellow
Write-Host "  2. Windows 防火墙是否放行 8001 端口:" -ForegroundColor Yellow
Write-Host "     New-NetFirewallRule -DisplayName 'TrendRadarCN 8001' -Direction Inbound -Protocol TCP -LocalPort 8001 -Action Allow -Profile Private" -ForegroundColor Gray
Write-Host ""
