<#
.SYNOPSIS
  启动 AstrBot + NapCat（docker compose up -d），首次运行时自动生成 .env。
.PARAMETER Pull
  启动前拉取镜像（升级镜像版本后使用）。
#>
param([switch]$Pull)
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    Write-Error '未找到 docker 命令。请先安装并启动 Docker Desktop：https://docs.docker.com/desktop/'
}
docker info --format '{{.ServerVersion}}' *> $null
if ($LASTEXITCODE -ne 0) { Write-Error 'Docker 未运行。请先启动 Docker Desktop，等待其显示 "Engine running" 后重试。' }

if (-not (Test-Path (Join-Path $root '.env'))) {
    & (Join-Path $PSScriptRoot 'init-env.ps1')
    Write-Host ''
    Write-Host '[!] 已生成 .env。请先编辑 .env 填写模型配置（也可稍后在 AstrBot WebUI 中配置），然后重新运行本脚本。'
    exit 0
}
& (Join-Path $PSScriptRoot 'init-env.ps1') | Out-Null  # 确保 data 目录存在

if ($Pull) { docker compose pull; if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE } }
docker compose up -d
if ($LASTEXITCODE -ne 0) {
    Write-Host ''
    Write-Host '[x] 启动失败。init 服务的输出：'
    docker compose logs --no-color init
    exit 1
}

docker compose ps
$astrbotPort = (Select-String -Path .env -Pattern '^ASTRBOT_WEBUI_PORT=(\d+)' | ForEach-Object { $_.Matches[0].Groups[1].Value }) | Select-Object -First 1
$napcatPort = (Select-String -Path .env -Pattern '^NAPCAT_WEBUI_PORT=(\d+)' | ForEach-Object { $_.Matches[0].Groups[1].Value }) | Select-Object -First 1
if (-not $astrbotPort) { $astrbotPort = 6185 }
if (-not $napcatPort) { $napcatPort = 6099 }

Write-Host ''
Write-Host "NapCat WebUI（扫码登录 QQ）：http://127.0.0.1:$napcatPort/webui   令牌见 .env 中的 NAPCAT_WEBUI_TOKEN"
Write-Host "AstrBot WebUI（模型/人格/规则）：http://127.0.0.1:$astrbotPort   用户名 astrbot，初始密码见："
Write-Host '    docker compose logs astrbot | Select-String -Pattern "password"'
Write-Host '查看日志：docker compose logs -f astrbot napcat      诊断：.\scripts\doctor.ps1'
