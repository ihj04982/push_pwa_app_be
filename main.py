"""
Push PWA Backend: receives notification content from frontend, reads FCM tokens
from Firestore, and sends push via FCM to those devices.
"""
import logging
import os
import time
from collections import defaultdict
from typing import Optional

from dotenv import load_dotenv

_BASE_DIR = os.path.dirname(os.path.abspath(os.path.realpath(__file__)))
load_dotenv(dotenv_path=os.path.join(_BASE_DIR, ".env"))

import firebase_admin
from firebase_admin import credentials, firestore, messaging
from fastapi import FastAPI, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
CORS_ORIGINS = [
    "http://localhost:5173",
    "http://localhost:4173",
    "http://127.0.0.1:5173",
    "http://127.0.0.1:4173",
]
FCM_TOKENS_COLLECTION = "fcmTokens"
MAX_TOKENS_PER_REQUEST = 100

# Optional API key: if PUSH_API_KEY is set, requests to /api/send-push must send X-API-Key header.
PUSH_API_KEY = (os.environ.get("PUSH_API_KEY") or "").strip()

# Simple in-memory rate limit: max 30 send-push requests per minute per client IP.
RATE_LIMIT_REQUESTS = 30
RATE_LIMIT_WINDOW_SEC = 60
_rate_limit_buckets: dict[str, list[float]] = defaultdict(list)


def _rate_limit_check(client_ip: str) -> bool:
    """Return True if request is allowed, False if over limit."""
    now = time.monotonic()
    bucket = _rate_limit_buckets[client_ip]
    bucket[:] = [t for t in bucket if now - t < RATE_LIMIT_WINDOW_SEC]
    if len(bucket) >= RATE_LIMIT_REQUESTS:
        return False
    bucket.append(now)
    return True


app = FastAPI(title="Push PWA Backend")
app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_origin_regex=r"https://.*\.vercel\.app",
    allow_credentials=True,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)

# ---------------------------------------------------------------------------
# Firebase
# ---------------------------------------------------------------------------
_firebase_initialized = False


def _resolve_credentials_path(path: str) -> str:
    """상대 경로를 절대 경로로 변환. main.py 기준 → CWD 기준."""
    path = path.strip()
    if not path or os.path.isabs(path):
        return path
    candidate = os.path.normpath(os.path.join(_BASE_DIR, path))
    if os.path.isfile(candidate):
        return os.path.abspath(candidate)
    return os.path.abspath(path)


def _try_fallback_credentials_path(path: str) -> str:
    """keys/serviceAccountKey.json 형태의 상대 경로일 때 main.py 기준 fallback 시도."""
    if not path or os.path.isfile(path):
        return path
    normalized = path.replace("\\", "/")
    if not normalized.endswith("keys/serviceAccountKey.json"):
        return path
    fallback = os.path.join(_BASE_DIR, "keys", "serviceAccountKey.json")
    if os.path.isfile(fallback):
        return os.path.abspath(fallback)
    return path


def _get_firebase_app():
    global _firebase_initialized
    if _firebase_initialized:
        return firebase_admin.get_app()
    path = (
        os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
        or os.environ.get("FIREBASE_SERVICE_ACCOUNT_PATH")
        or ""
    ).strip()
    path = _resolve_credentials_path(path) if path else ""
    path = _try_fallback_credentials_path(path)
    if path and os.path.isfile(path):
        os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = path
        firebase_admin.initialize_app(credentials.Certificate(path))
    else:
        firebase_admin.initialize_app()
    _firebase_initialized = True
    return firebase_admin.get_app()


def _collect_fcm_tokens(db, device_name: Optional[str]) -> list[str]:
    """Firestore fcmTokens에서 token 필드만 수집. device_name이 있으면 해당 기기만."""
    col_ref = db.collection(FCM_TOKENS_COLLECTION)
    if device_name and device_name.strip():
        query = col_ref.where("deviceName", "==", device_name.strip()).limit(
            MAX_TOKENS_PER_REQUEST
        )
    else:
        query = col_ref.limit(MAX_TOKENS_PER_REQUEST)
    tokens: list[str] = []
    for doc in query.stream():
        data = doc.to_dict()
        token = data.get("token") if isinstance(data, dict) else None
        if isinstance(token, str) and token.strip():
            tokens.append(token.strip())
    return tokens


def _send_messages_to_tokens(tokens: list[str], title: str, body: str) -> tuple[int, int]:
    """각 토큰으로 FCM 발송. (success_count, failure_count) 반환."""
    success = failure = 0
    for token in tokens:
        try:
            messaging.send(
                messaging.Message(
                    notification=messaging.Notification(
                        title=title[:200],
                        body=body[:1000],
                    ),
                    token=token,
                )
            )
            success += 1
        except Exception as e:
            failure += 1
            logger.warning("FCM send failed for token %s...: %s", token[:16] if token else "", e)
    return success, failure


class SendPushRequest(BaseModel):
    title: str = Field(..., min_length=1, max_length=200, description="알림 제목")
    body: str = Field(..., min_length=1, max_length=1000, description="알림 본문")
    device_name: Optional[str] = Field(
        default=None,
        max_length=100,
        description="대상 장치명. 생략 시 등록된 전체 기기로 발송",
        alias="deviceName",
    )

    model_config = {"populate_by_name": True}


class SendPushResponse(BaseModel):
    success_count: int
    failure_count: int
    total: int
    message: str


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/api/send-push", response_model=SendPushResponse)
def send_push(request: Request, req: SendPushRequest):
    client_ip = request.client.host if request.client else "unknown"
    if not _rate_limit_check(client_ip):
        return JSONResponse(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            content={"detail": "Too many requests. Try again later."},
        )
    if PUSH_API_KEY and request.headers.get("X-API-Key") != PUSH_API_KEY:
        return JSONResponse(
            status_code=status.HTTP_401_UNAUTHORIZED,
            content={"detail": "Invalid or missing API key."},
        )
    _get_firebase_app()
    db = firestore.client()
    tokens = _collect_fcm_tokens(db, req.device_name)
    if not tokens:
        return SendPushResponse(
            success_count=0,
            failure_count=0,
            total=0,
            message="발송할 FCM 토큰이 없습니다. deviceName을 확인하거나, 앱에서 먼저 알림 권한 및 토큰 등록을 해 주세요.",
        )
    success, failure = _send_messages_to_tokens(tokens, req.title, req.body)
    return SendPushResponse(
        success_count=success,
        failure_count=failure,
        total=len(tokens),
        message=f"발송 완료: 성공 {success}, 실패 {failure} (총 {len(tokens)}개 기기)",
    )
