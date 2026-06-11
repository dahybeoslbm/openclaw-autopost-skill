#!/bin/bash
NEW_ID=$1
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ENV_FILE="$SCRIPT_DIR/../.env"

if [ -z "$NEW_ID" ]; then
  echo "Usage: ./add_chat_id.sh <telegram_user_id>"
  exit 1
fi

CURRENT=$(grep "^CHAT_IDS=" "$ENV_FILE" 2>/dev/null | cut -d= -f2)

if [[ ",$CURRENT," == *",$NEW_ID,"* ]]; then
  echo "ID $NEW_ID đã tồn tại"
  exit 0
fi

if [ -z "$CURRENT" ]; then
  if grep -q "^CHAT_IDS=" "$ENV_FILE" 2>/dev/null; then
    if [[ "$OSTYPE" == "darwin"* ]]; then
      sed -i '' "s/^CHAT_IDS=.*/CHAT_IDS=$NEW_ID/" "$ENV_FILE"
    else
      sed -i "s/^CHAT_IDS=.*/CHAT_IDS=$NEW_ID/" "$ENV_FILE"
    fi
  else
    echo "CHAT_IDS=$NEW_ID" >> "$ENV_FILE"
  fi
else
  if [[ "$OSTYPE" == "darwin"* ]]; then
    sed -i '' "s/^CHAT_IDS=.*/CHAT_IDS=$CURRENT,$NEW_ID/" "$ENV_FILE"
  else
    sed -i "s/^CHAT_IDS=.*/CHAT_IDS=$CURRENT,$NEW_ID/" "$ENV_FILE"
  fi
fi

echo "✅ Đã thêm CHAT_ID=$NEW_ID vào .env"
