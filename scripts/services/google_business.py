"""
services/google_business.py — Đăng bài lên Google Business Profile (Google Maps).
Sử dụng cấu hình refresh token có sẵn trong .env.
"""
import requests
from config import GoogleBusinessConfig
from utils.logger import get_logger
from utils.models import GoogleBusinessPostResult

logger = get_logger(__name__)

class GoogleBusinessService:
    def __init__(self, config: GoogleBusinessConfig):
        self._config = config
        self._access_token = None
        self._account_name = None
        self._location_name = None

    def _get_access_token(self) -> str:
        if self._access_token:
            return self._access_token
            
        logger.info("  → [Google Business] Lấy access_token mới từ refresh_token...")
        payload = {
            "client_id": self._config.client_id,
            "client_secret": self._config.client_secret,
            "refresh_token": self._config.refresh_token,
            "grant_type": "refresh_token"
        }
        resp = requests.post("https://oauth2.googleapis.com/token", data=payload, timeout=15)
        resp.raise_for_status()
        self._access_token = resp.json().get("access_token")
        return self._access_token

    def _get_location_name(self, token: str) -> str:
        if self._location_name:
            return self._location_name
            
        logger.info("  → [Google Business] Đang fetch thông tin Account và Location...")
        headers = {"Authorization": f"Bearer {token}"}
        
        # Lấy account
        acc_resp = requests.get("https://mybusinessaccountmanagement.googleapis.com/v1/accounts", headers=headers, timeout=15)
        acc_resp.raise_for_status()
        accounts = acc_resp.json().get("accounts", [])
        if not accounts:
            raise RuntimeError("Không tìm thấy account Google Business nào liên kết với token này.")
            
        self._account_name = accounts[0]["name"]
        
        # Lấy location
        loc_resp = requests.get(f"https://mybusinessbusinessinformation.googleapis.com/v1/{self._account_name}/locations?readMask=name", headers=headers, timeout=15)
        loc_resp.raise_for_status()
        locations = loc_resp.json().get("locations", [])
        if not locations:
            raise RuntimeError(f"Không tìm thấy location nào thuộc account {self._account_name}.")
            
        self._location_name = locations[0]["name"]
        return self._location_name

    def post_local_post(
        self, 
        text: str, 
        image_urls: list[str] | None = None,
        action_url: str = "https://timchuyenbay.vn/"
    ) -> GoogleBusinessPostResult:
        try:
            token = self._get_access_token()
            location_name = self._get_location_name(token)
            
            payload = {
                "languageCode": "vi-VN",
                "summary": text,
                "topicType": "STANDARD",
                "callToAction": {
                    "actionType": "LEARN_MORE",
                    "url": action_url
                }
            }
            
            if image_urls and len(image_urls) > 0:
                payload["media"] = [
                    {
                        "mediaFormat": "PHOTO",
                        "sourceUrl": image_urls[0]
                    }
                ]
                
            headers = {
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json"
            }
            
            logger.info("  → [Google Business] Đang đăng bài lên %s...", location_name)
            
            # API v4 yêu cầu full path: accounts/{accountId}/locations/{locationId}/localPosts
            # Account Management API trả về account_name là "accounts/xxx"
            # Business Info API v1 trả về location_name là "locations/xxx"
            # Do đó ta ghép lại
            post_url = f"https://mybusiness.googleapis.com/v4/{self._account_name}/{location_name}/localPosts"
            logger.info("  → [Google Business] Payload: %s", payload)
            resp = requests.post(post_url, headers=headers, json=payload, timeout=30)
            
            if resp.ok:
                post_id = resp.json().get("name", "")
                logger.info("  → [Google Business] ✅ Thành công | post_id=%s", post_id)
                return GoogleBusinessPostResult(
                    location_id=location_name,
                    status="success",
                    post_id=post_id
                )
            else:
                logger.error("  → [Google Business] ❌ Lỗi HTTP %d: %s", resp.status_code, resp.text)
                return GoogleBusinessPostResult(
                    location_id=location_name,
                    status="error",
                    error=f"HTTP {resp.status_code}: {resp.text}"
                )
                
        except Exception as exc:
            logger.error("  → [Google Business] ❌ Lỗi Exception: %s", exc)
            return GoogleBusinessPostResult(
                location_id=self._location_name or "unknown",
                status="error",
                error=str(exc)
            )
