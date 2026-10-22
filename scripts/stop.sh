#!/usr/bin/env bash
# 停止并移除容器（docker compose down）。data/ 中的配置、QQ 登录态与聊天记录会保留。
set -euo pipefail
cd "$(dirname "$0")/.."
docker compose down
