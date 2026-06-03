#!/bin/bash

BASE_URL="http://localhost:8001/index.php?entryPoint=epZaloPost"
API_KEY="DAHYVAN"
PHOTO_URL="https://drive.usercontent.google.com/download?id=1kJ97yDfV2dN4onQtqEl-t5mQbomjhnbv&export=view&authuser=0"
APP_ID="1909878231918719642"

echo "--- TEST 0: Base request (Kiểm chứng ảnh) ---"
curl -s -X POST "$BASE_URL" \
-H "Api-Key: $API_KEY" \
-H "Content-Type: application/json" \
-d '{
    "method": "createArticle",
    "params": {
        "app_id": "'"$APP_ID"'",
        "type": "normal",
        "title": "Test Bài Viết Dạng Normal",
        "author": "Hệ thống Booking",
        "description": "Đây là bài viết dạng thường dùng ảnh cover để test API.",
        "cover": {
            "cover_type": "photo",
            "photo_url": "'"$PHOTO_URL"'",
            "status": "show"
        },
        "body": [
            {"type": "text", "content": "Test Base"}
        ],
        "status": "hide",
        "comment": "hide"
    }
}' | jq .

echo ""
echo "--- TEST 1: Tham số action (object) ở root params ---"
curl -s -X POST "$BASE_URL" \
-H "Api-Key: $API_KEY" \
-H "Content-Type: application/json" \
-d '{
    "method": "createArticle",
    "params": {
        "app_id": "'"$APP_ID"'",
        "type": "normal",
        "title": "Test CTA 1",
        "description": "Test description 123",
        "cover": {
            "cover_type": "photo",
            "photo_url": "'"$PHOTO_URL"'",
            "status": "show"
        },
        "body": [{"type": "text", "content": "Test CTA 1"}],
        "status": "hide",
        "comment": "hide",
        "action": {
            "type": "link",
            "link": "https://timchuyenbay.vn/",
            "label": "Click me 1"
        }
    }
}' | jq .

echo ""
echo "--- TEST 2: Tham số action_link và action_text ---"
curl -s -X POST "$BASE_URL" \
-H "Api-Key: $API_KEY" \
-H "Content-Type: application/json" \
-d '{
    "method": "createArticle",
    "params": {
        "app_id": "'"$APP_ID"'",
        "type": "normal",
        "title": "Test CTA 2",
        "description": "Test description 123",
        "cover": {
            "cover_type": "photo",
            "photo_url": "'"$PHOTO_URL"'",
            "status": "show"
        },
        "body": [{"type": "text", "content": "Test CTA 2"}],
        "status": "hide",
        "comment": "hide",
        "action_link": "https://timchuyenbay.vn/",
        "action_text": "Click me 2"
    }
}' | jq .
