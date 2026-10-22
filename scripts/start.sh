#!/usr/bin/env bash
# 启动 AstrBot + NapCat（docker compose up -d），首次运行时自动生成 .env。
# 用法：./scripts/start.sh [--pull]
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

command -v docker >/dev/null 2>&1 || { echo "未找到 docker 命令，请先安装 Docker Engine + Compose 插件" >&2; exit 1; }
docker info >/dev/null 2>&1 || { echo "Docker 未运行或当前用户无权限（可尝试 sudo 或将用户加入 docker 组）" >&2; exit 1; }

if [ ! -f .env ]; then
  "$ROOT/scripts/init-env.sh"
  echo
  echo "[!] 已生成 .env。请先编辑 .env 填写模型配置（也可稍后在 AstrBot WebUI 中配置），然后重新运行本脚本。"
  exit 0
fi
"$ROOT/scripts/init-env.sh" >/dev/null   # 确保 data 目录存在

if [ "${1:-}" = "--pull" ]; then docker compose pull; fi
if ! docker compose up -d; then
  echo; echo "[x] 启动失败。init 服务的输出："
  docker compose logs --no-color init
  exit 1
fi

docker compose ps
ASTRBOT_PORT="$(sed -n 's/^ASTRBOT_WEBUI_PORT=\([0-9]*\).*/\1/p' .env | head -n1)"
NAPCAT_PORT="$(sed -n 's/^NAPCAT_WEBUI_PORT=\([0-9]*\).*/\1/p' .env | head -n1)"
echo
echo "NapCat WebUI（扫码登录 QQ）：http://127.0.0.1:${NAPCAT_PORT:-6099}/webui   令牌见 .env 中的 NAPCAT_WEBUI_TOKEN"
echo "AstrBot WebUI（模型/人格/规则）：http://127.0.0.1:${ASTRBOT_PORT:-6185}   用户名 astrbot，初始密码见："
echo "    docker compose logs astrbot | grep -i password"
echo "查看日志：docker compose logs -f astrbot napcat      诊断：./scripts/doctor.sh"
