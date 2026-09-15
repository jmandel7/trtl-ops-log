#!/usr/bin/env bash
set -e

SECRET_FILE="/data/secret_path.txt"

if [ ! -f "$SECRET_FILE" ]; then
    SECRET="private_$(head -c 24 /dev/urandom | base64 | tr -dc 'a-zA-Z0-9' | head -c 24)"
    echo "$SECRET" > "$SECRET_FILE"
    echo "[INFO] Generated new secret path, persisted to $SECRET_FILE"
else
    SECRET=$(cat "$SECRET_FILE")
    echo "[INFO] Using existing secret path from $SECRET_FILE"
fi

export TRTL_SECRET_PATH="$SECRET"
export TRTL_OPS_DB="/data/trtl_ops.sqlite3"
export PORT="9584"

echo ""
echo "================================================================================"
echo "🔐 TRTL Ops Log URL: http://<home-assistant-ip>:9584/${SECRET}"
echo ""
echo "   Secret Path: /${SECRET}"
echo ""
echo "   ⚠️  IMPORTANT: Copy this exact URL - the secret path is required!"
echo "   💡 This path is auto-generated and persisted to /data/secret_path.txt"
echo "================================================================================"
echo ""

exec python3 /app/server.py
