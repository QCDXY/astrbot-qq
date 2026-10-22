#!/usr/bin/env bash
# 从 .env.example 生成 .env，并为 OneBot 访问令牌与 NapCat WebUI 令牌填入随机值。
# 已存在 .env 时不做任何修改。
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
ENV_FILE="$ROOT/.env"

rand_hex() { # $1 = 字节数
  if command -v openssl >/dev/null 2>&1; then openssl rand -hex "$1"
  else head -c "$1" /dev/urandom | od -An -tx1 | tr -d ' \n'; fi
}

mkdir -p "$ROOT/data/astrbot" "$ROOT/data/napcat/config" "$ROOT/data/napcat/qq"

if [ -f "$ENV_FILE" ]; then
  echo "[skip] .env 已存在，未做修改：$ENV_FILE"
  exit 0
fi

sed -e "s|^ONEBOT_ACCESS_TOKEN=.*|ONEBOT_ACCESS_TOKEN=$(rand_hex 24)|" \
    -e "s|^NAPCAT_WEBUI_TOKEN=.*|NAPCAT_WEBUI_TOKEN=$(rand_hex 16)|" \
    -e "s|^NAPCAT_UID=.*|NAPCAT_UID=$(id -u)|" \
    -e "s|^NAPCAT_GID=.*|NAPCAT_GID=$(id -g)|" \
    "$ROOT/.env.example" > "$ENV_FILE"
chmod 600 "$ENV_FILE"

echo "[ok] 已生成 $ENV_FILE（已填入随机令牌，权限 600）"
echo "     下一步：编辑 .env 填写 LLM_API_KEY / LLM_BASE_URL / LLM_MODEL（可选 ADMIN_QQ、QQ_ACCOUNT），"
echo "     然后运行 ./scripts/start.sh"
