<#
.SYNOPSIS
  停止并移除容器（docker compose down）。data\ 中的配置、QQ 登录态与聊天记录会保留。
#>
$ErrorActionPreference = 'Stop'
Set-Location (Split-Path -Parent $PSScriptRoot)
docker compose down
exit $LASTEXITCODE
