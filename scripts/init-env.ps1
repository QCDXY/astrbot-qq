<#
.SYNOPSIS
  从 .env.example 生成 .env，并为 OneBot 访问令牌与 NapCat WebUI 令牌填入随机值。
  已存在 .env 时不做任何修改。兼容 Windows PowerShell 5.1 与 PowerShell 7。
#>
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$envFile = Join-Path $root '.env'
$example = Join-Path $root '.env.example'

function New-RandomToken([int]$bytes = 24) {
    $buf = New-Object byte[] $bytes
    $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    try { $rng.GetBytes($buf) } finally { $rng.Dispose() }
    ($buf | ForEach-Object { $_.ToString('x2') }) -join ''
}

foreach ($d in 'data\astrbot', 'data\napcat\config', 'data\napcat\qq') {
    New-Item -ItemType Directory -Force -Path (Join-Path $root $d) | Out-Null
}

if (Test-Path $envFile) {
    Write-Host "[skip] .env 已存在，未做修改：$envFile"
    exit 0
}

$content = [IO.File]::ReadAllText($example)
$content = $content -replace '(?m)^ONEBOT_ACCESS_TOKEN=.*$', ("ONEBOT_ACCESS_TOKEN=" + (New-RandomToken))
$content = $content -replace '(?m)^NAPCAT_WEBUI_TOKEN=.*$', ("NAPCAT_WEBUI_TOKEN=" + (New-RandomToken 16))
# 写入无 BOM 的 UTF-8，docker compose 可正确解析
[IO.File]::WriteAllText($envFile, $content, (New-Object System.Text.UTF8Encoding $false))

Write-Host "[ok] 已生成 $envFile（已填入随机令牌）"
Write-Host "     下一步：编辑 .env 填写 LLM_API_KEY / LLM_BASE_URL / LLM_MODEL（可选 ADMIN_QQ、QQ_ACCOUNT），"
Write-Host "     然后运行 .\scripts\start.ps1"
