#!/usr/bin/env bash
# run.sh — Chạy auto-travel-blogger nhanh nhất có thể.
#
# Chiến lược (theo thứ tự ưu tiên):
#   1. docker exec   → ~0.3s  (container đang chạy sẵn)
#   2. docker exec   → ~5s    (tự start daemon rồi exec)
#   3. docker compose run --rm (fallback nếu exec thất bại)
#
# Cách dùng:
#   ./run.sh "đăng bài về Đà Lạt lên Facebook"
#   ./run.sh "1"           # trả lời chọn bài (two-turn)
#   ./run.sh --start       # chỉ khởi động daemon, không chạy lệnh
#   ./run.sh --stop        # dừng daemon
#   ./run.sh --status      # kiểm tra trạng thái
# ─────────────────────────────────────────────────────────────────────────────

set -euo pipefail

CONTAINER_NAME="auto-travel-blogger"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON_CMD="python /app/scripts/blogger.py"
# CHAT_ID được truyền vào từ env (do caller set), forward vào docker exec
CHAT_ID_FLAG=""  
[[ -n "${CHAT_ID:-}" ]] && CHAT_ID_FLAG="-e CHAT_ID=${CHAT_ID}"

# ── Helpers ──────────────────────────────────────────────────────────────────

is_running() {
    docker inspect -f '{{.State.Running}}' "$CONTAINER_NAME" 2>/dev/null | grep -q "^true$"
}

start_daemon() {
    echo "🚀 Khởi động daemon container..." >&2
    (cd "$SCRIPT_DIR" && docker compose up -d auto-travel-blogger) >&2

    # Chờ container sẵn sàng (tối đa 20s)
    local waited=0
    while ! is_running; do
        if [[ $waited -ge 20 ]]; then
            echo "❌ Timeout: container không khởi động được trong 20s" >&2
            return 1
        fi
        sleep 1
        waited=$((waited + 1))
    done
    echo "✅ Daemon sẵn sàng (${waited}s)" >&2
}

# ── Subcommands ───────────────────────────────────────────────────────────────

case "${1:-}" in
    --start)
        start_daemon
        echo "Container '$CONTAINER_NAME' đang chạy." >&2
        exit 0
        ;;
    --stop)
        echo "🛑 Dừng daemon..." >&2
        (cd "$SCRIPT_DIR" && docker compose stop auto-travel-blogger) >&2
        exit 0
        ;;
    --status)
        if is_running; then
            echo "✅ Container '$CONTAINER_NAME' đang chạy"
        else
            echo "⭕ Container '$CONTAINER_NAME' chưa chạy"
        fi
        exit 0
        ;;
    --rebuild)
        echo "🔨 Rebuild image và restart daemon..." >&2
        (cd "$SCRIPT_DIR" && docker compose build auto-travel-blogger && docker compose up -d auto-travel-blogger) >&2
        echo "✅ Rebuild xong" >&2
        exit 0
        ;;
esac

# ── Cần ít nhất 1 argument ───────────────────────────────────────────────────

if [[ $# -eq 0 ]]; then
    echo "Cách dùng: $0 \"câu lệnh\"" >&2
    echo "           $0 --start | --stop | --status | --rebuild" >&2
    exit 1
fi

USER_CMD="$1"

# ── Thử docker exec (fast path) ───────────────────────────────────────────────

if is_running; then
    # Container sẵn sàng → exec ngay, không overhead
    docker exec -i $CHAT_ID_FLAG "$CONTAINER_NAME" $PYTHON_CMD "$USER_CMD"
    exit $?
fi

# ── Container chưa chạy → tự start daemon ────────────────────────────────────

echo "⚡ Container chưa chạy. Đang khởi động daemon lần đầu..." >&2

if start_daemon; then
    docker exec -i $CHAT_ID_FLAG "$CONTAINER_NAME" $PYTHON_CMD "$USER_CMD"
    exit $?
fi

# ── Fallback: one-shot run (chậm nhất, chỉ dùng khi daemon lỗi) ──────────────

echo "⚠️  Fallback: dùng docker compose run (chậm hơn)..." >&2
(cd "$SCRIPT_DIR" && docker compose run --rm auto-travel-blogger-run "$USER_CMD")
