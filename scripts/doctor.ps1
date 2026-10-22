<#
.SYNOPSIS
  诊断 AstrBot + NapCat 部署：Docker、.env、Compose 配置、容器状态、两端令牌/地址、
  端口暴露、容器网络与日志中的连接状态。只读，不修改任何文件。
#>
$ErrorActionPreference = 'Continue'
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
$script:failed = 0

function Pass($m) { Write-Host "[ OK ] $m" -ForegroundColor Green }
function Warn($m) { Write-Host "[WARN] $m" -ForegroundColor Yellow }
function Fail($m) { Write-Host "[FAIL] $m" -ForegroundColor Red; $script:failed++ }

# 1. Docker
if (-not (Get-Command docker -ErrorAction SilentlyContinue)) { Fail '未找到 docker 命令（请安装 Docker Desktop）'; exit 1 }
$ver = docker info --format '{{.ServerVersion}}' 2>$null
if ($LASTEXITCODE -ne 0) { Fail 'Docker 引擎未运行（请启动 Docker Desktop）'; exit 1 }
Pass "Docker 引擎 $ver"

# 2. .env
if (-not (Test-Path .env)) { Fail '.env 不存在：运行 .\scripts\init-env.ps1'; exit 1 }
$envMap = @{}
Get-Content .env -Encoding UTF8 | Where-Object { $_ -match '^\s*([A-Z0-9_]+)=(.*)$' } | ForEach-Object { $envMap[$Matches[1]] = $Matches[2].Trim() }
$token = $envMap['ONEBOT_ACCESS_TOKEN']
if (-not $token -or $token -like 'change-me*' -or $token.Length -lt 16) { Fail 'ONEBOT_ACCESS_TOKEN 未设置/仍为示例值/过短' } else { Pass 'ONEBOT_ACCESS_TOKEN 已设置' }
if (-not $envMap['LLM_API_KEY']) { Warn 'LLM_API_KEY 为空：机器人无法调用模型（可在 .env 或 AstrBot WebUI 中配置）' } else { Pass 'LLM_API_KEY 已设置（内容不显示）' }
if ($envMap['WEBUI_BIND_HOST'] -and $envMap['WEBUI_BIND_HOST'] -ne '127.0.0.1') { Warn "管理界面绑定到 $($envMap['WEBUI_BIND_HOST'])，请确认已有防火墙/鉴权保护" }

# 3. Compose 配置
docker compose config --quiet
if ($LASTEXITCODE -eq 0) { Pass 'docker compose config 校验通过' } else { Fail 'compose.yaml 校验失败（见上方错误）' }

# 4. 两端连接配置一致性（使用 AstrBot 镜像中的 Python，只读检查）
docker compose run --rm --no-deps init --astrbot-data /AstrBot/data --napcat-config /napcat/config --check
if ($LASTEXITCODE -eq 0) { Pass 'AstrBot 与 NapCat 的令牌/地址一致' } else { Fail '连接配置不一致或未初始化：运行 docker compose up -d 让 init 自动同步' }

# 5. 容器状态
foreach ($svc in 'astrbot', 'napcat') {
    $state = docker inspect -f '{{.State.Status}} {{if .State.Health}}{{.State.Health.Status}}{{end}} restarts={{.RestartCount}}' $svc 2>$null
    if ($LASTEXITCODE -ne 0) { Fail "容器 $svc 不存在（尚未启动？）"; continue }
    if ($state -like 'running*') { Pass "容器 $svc：$state" } else { Fail "容器 $svc：$state" }
}

# 6. 端口暴露
$pub = docker compose port astrbot 6199 2>$null
if ($pub) { Fail "6199 被映射到宿主机（$pub），应只在容器网络内使用" } else { Pass '6199 未暴露到宿主机' }
foreach ($p in @(@('astrbot', 6185), @('napcat', 6099))) {
    $b = docker compose port $p[0] $p[1] 2>$null
    if (-not $b) { continue }
    if ($b -like '127.0.0.1:*') { Pass "$($p[0]) WebUI 仅绑定本机：$b" } else { Warn "$($p[0]) WebUI 绑定为 $b" }
}

# 7. 容器网络：NapCat 能否解析并连通 astrbot:6199
docker exec napcat bash -c 'exec 3<>/dev/tcp/astrbot/6199' 2>$null
if ($LASTEXITCODE -eq 0) { Pass 'napcat -> astrbot:6199 TCP 连通' } else { Warn 'napcat 无法连接 astrbot:6199（容器未运行或 AstrBot 平台未启动）' }

# 8. 日志关键字
$alog = docker logs --tail 400 astrbot 2>&1 | Out-String
if ($alog -match '适配器已连接') { Pass 'AstrBot 日志：OneBot 适配器已连接（NapCat 已接入）' } else { Warn 'AstrBot 日志中未见 "适配器已连接"：NapCat 可能尚未登录 QQ 或令牌不匹配' }
if ($alog -match '(?i)403|401|Forbidden|Unauthorized') { Warn 'AstrBot 日志中出现 401/403：检查两端令牌' }
$nlog = docker logs --tail 400 napcat 2>&1 | Out-String
# 已连接说明已登录；否则日志里的旧二维码提示才有意义
if ($alog -notmatch '适配器已连接' -and $nlog -match '(?i)二维码|qrcode') { Warn 'NapCat 正在等待扫码登录：打开 NapCat WebUI 扫码' }
if ($nlog -match '(?i)ECONNREFUSED|ENOTFOUND') { Warn 'NapCat 连接 AstrBot 失败（ECONNREFUSED/ENOTFOUND），将按 reconnectInterval 自动重试' }

Write-Host ''
if ($script:failed -gt 0) { Write-Host "共 $script:failed 项失败。详见 docs/troubleshooting.md" -ForegroundColor Red; exit 1 }
Write-Host '诊断完成，未发现阻断性问题。' -ForegroundColor Green
