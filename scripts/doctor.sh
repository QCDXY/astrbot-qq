#!/usr/bin/env bash
# 诊断 AstrBot + NapCat 部署：Docker、.env、Compose 配置、容器状态、两端令牌/地址、
# 端口暴露、容器网络与日志中的连接状态。只读，不修改任何文件。
set -uo pipefail
# Git Bash (Windows) 会把 /AstrBot/data 这类容器内路径改写成 Windows 路径，这里关闭该转换
export MSYS_NO_PATHCONV=1
cd "$(dirname "$0")/.."
failed=0
pass() { printf '\033[32m[ OK ]\033[0m %s\n' "$1"; }
warn() { printf '\033[33m[WARN]\033[0m %s\n' "$1"; }
fail() { printf '\033[31m[FAIL]\033[0m %s\n' "$1"; failed=$((failed + 1)); }

command -v docker >/dev/null 2>&1 || { fail "未找到 docker 命令"; exit 1; }
ver="$(docker info --format '{{.ServerVersion}}' 2>/dev/null)" || { fail "Docker 引擎未运行或无权限"; exit 1; }
pass "Docker 引擎 $ver"

[ -f .env ] || { fail ".env 不存在：运行 ./scripts/init-env.sh"; exit 1; }
getenv() { sed -n "s/^$1=//p" .env | head -n1 | tr -d '\r'; }
token="$(getenv ONEBOT_ACCESS_TOKEN)"
if [ -z "$token" ] || [ "${token#change-me}" != "$token" ] || [ "${#token}" -lt 16 ]; then
  fail "ONEBOT_ACCESS_TOKEN 未设置/仍为示例值/过短"
else pass "ONEBOT_ACCESS_TOKEN 已设置"; fi
if [ -z "$(getenv LLM_API_KEY)" ]; then warn "LLM_API_KEY 为空：机器人无法调用模型（可在 .env 或 AstrBot WebUI 中配置）"
else pass "LLM_API_KEY 已设置（内容不显示）"; fi
bind="$(getenv WEBUI_BIND_HOST)"
if [ -n "$bind" ] && [ "$bind" != "127.0.0.1" ]; then warn "管理界面绑定到 $bind，请确认已有防火墙/鉴权保护"; fi

if docker compose config --quiet; then pass "docker compose config 校验通过"; else fail "compose.yaml 校验失败"; fi

if docker compose run --rm --no-deps init --astrbot-data /AstrBot/data --napcat-config /napcat/config --check; then
  pass "AstrBot 与 NapCat 的令牌/地址一致"
else fail "连接配置不一致或未初始化：运行 docker compose up -d 让 init 自动同步"; fi

for svc in astrbot napcat; do
  if state="$(docker inspect -f '{{.State.Status}} {{if .State.Health}}{{.State.Health.Status}}{{end}} restarts={{.RestartCount}}' "$svc" 2>/dev/null)"; then
    case "$state" in running*) pass "容器 $svc：$state" ;; *) fail "容器 $svc：$state" ;; esac
  else fail "容器 $svc 不存在（尚未启动？）"; fi
done

pub="$(docker compose port astrbot 6199 2>/dev/null || true)"
if [ -n "$pub" ]; then fail "6199 被映射到宿主机（$pub），应只在容器网络内使用"; else pass "6199 未暴露到宿主机"; fi
for pair in astrbot:6185 napcat:6099; do
  b="$(docker compose port "${pair%%:*}" "${pair##*:}" 2>/dev/null || true)"
  [ -z "$b" ] && continue
  case "$b" in 127.0.0.1:*) pass "${pair%%:*} WebUI 仅绑定本机：$b" ;; *) warn "${pair%%:*} WebUI 绑定为 $b" ;; esac
done

if docker exec napcat bash -c 'exec 3<>/dev/tcp/astrbot/6199' 2>/dev/null; then pass "napcat -> astrbot:6199 TCP 连通"
else warn "napcat 无法连接 astrbot:6199（容器未运行或 AstrBot 平台未启动）"; fi

alog="$(docker logs --tail 400 astrbot 2>&1 || true)"
if grep -q '适配器已连接' <<<"$alog"; then pass "AstrBot 日志：OneBot 适配器已连接（NapCat 已接入）"
else warn "AstrBot 日志中未见 \"适配器已连接\"：NapCat 可能尚未登录 QQ 或令牌不匹配"; fi
grep -qiE '403|401|Forbidden|Unauthorized' <<<"$alog" && warn "AstrBot 日志中出现 401/403：检查两端令牌"
nlog="$(docker logs --tail 400 napcat 2>&1 || true)"
# 已连接说明已登录；否则日志里的旧二维码提示才有意义
if ! grep -q '适配器已连接' <<<"$alog" && grep -qiE '二维码|qrcode' <<<"$nlog"; then warn "NapCat 正在等待扫码登录：打开 NapCat WebUI 扫码"; fi
grep -qiE 'ECONNREFUSED|ENOTFOUND' <<<"$nlog" && warn "NapCat 连接 AstrBot 失败，将按 reconnectInterval 自动重试"

echo
if [ "$failed" -gt 0 ]; then echo "共 $failed 项失败。详见 docs/troubleshooting.md"; exit 1; fi
echo "诊断完成，未发现阻断性问题。"
