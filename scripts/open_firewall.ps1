# FacePass 局域网放行脚本：为 8010（HTTP）和 8443（HTTPS）添加入站规则
# 只对「本地子网」开放，不暴露到公网。需要管理员权限（会自动弹出 UAC）。
# 只查看状态：powershell -ExecutionPolicy Bypass -File scripts\open_firewall.ps1 -Check
param([switch]$Check)

$ErrorActionPreference = 'Stop'

if ($Check) {
    foreach ($port in 8010, 8443) {
        $name = "FacePass (TCP $port)"
        $rule = Get-NetFirewallRule -DisplayName $name -ErrorAction SilentlyContinue
        if ($rule) {
            Write-Host "[OK] $name 已存在（Action=$($rule.Action), Profile=$($rule.Profile)）"
        } else {
            Write-Host "[--] $name 不存在，局域网会被防火墙拦截"
        }
    }
    exit
}

$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = New-Object Security.Principal.WindowsPrincipal($identity)
$isAdmin = $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)

if (-not $isAdmin) {
    Write-Host '[!] 需要管理员权限，正在弹出 UAC 授权窗口…' -ForegroundColor Yellow
    Start-Process powershell.exe -Verb RunAs -ArgumentList @(
        '-NoProfile',
        '-ExecutionPolicy', 'Bypass',
        '-File', ('"' + $PSCommandPath + '"')
    ) | Out-Null
    exit
}

foreach ($port in @(8010, 8443)) {
    $name = "FacePass (TCP $port)"
    $existing = Get-NetFirewallRule -DisplayName $name -ErrorAction SilentlyContinue
    if ($existing) {
        Remove-NetFirewallRule -DisplayName $name -ErrorAction SilentlyContinue
    }
    New-NetFirewallRule `
        -DisplayName $name `
        -Description 'FacePass LAN access (local subnet only)' `
        -Direction Inbound `
        -Action Allow `
        -Protocol TCP `
        -LocalPort $port `
        -Profile Private, Public `
        -RemoteAddress LocalSubnet | Out-Null
    Write-Host "[OK] 已放行 TCP $port（仅本地子网）" -ForegroundColor Green
}

$ips = Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
    Where-Object { $_.IPAddress -notlike '127.*' -and $_.IPAddress -notlike '169.254.*' } |
    Select-Object -ExpandProperty IPAddress

Write-Host ''
Write-Host '局域网设备可访问：' -ForegroundColor Cyan
foreach ($ip in $ips) {
    Write-Host "  http://${ip}:8010"
    Write-Host "  https://${ip}:8443  （手机摄像头需要这个）"
}
Write-Host ''
Read-Host '按回车关闭'
