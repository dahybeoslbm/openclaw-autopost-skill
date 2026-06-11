#!/bin/bash
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
BASE_DIR="$SCRIPT_DIR/.."

# Xoá các file log quá 7 ngày
find "$BASE_DIR/logs" -type f -name "*.log" -mtime +7 -exec rm -f {} \;

# Xoá các file md và time quá 7 ngày
find "$BASE_DIR/output" -type f \( -name "*.md" -o -name "*.time" \) -mtime +7 -exec rm -f {} \;

echo "✅ Đã xoá các file log, md, time cũ hơn 7 ngày lúc $(date)"
