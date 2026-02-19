"""
Push PWA Backend: receives notification content from frontend, reads FCM tokens
from Firestore, and sends push via FCM to those devices.
"""
import os
from typing import Optional

import firebase_admin
from firebase_admin import credentials, firestore, messaging
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

app = FastAPI(title="Push PWA Backend")

# CORS: allow Vercel (*.vercel.app) and local dev
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",
        "http://localhost:4173",
        "http://127.0.0.1:5173",
        "http://127.0.0.1:4173",
    ],
    allow_origin_regex=r"https://.*\.vercel\.app",
    allow_credentials=True,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)

# Firebase Admin: initialize once
_firebase_initialized = False
FCM_TOKENS_COLLECTION = "fcmTokens"
MAX_TOKENS_PER_REQUEST = 100  # cap to avoid timeouts


def get_firebase_app():
    global _firebase_initialized
    if _firebase_initialized:
        return firebase_admin.get_app()
    path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS") or os.environ.get(
        "FIREBASE_SERVICE_ACCOUNT_PATH"
    )
    if path and os.path.isfile(path):
        cred = credentials.Certificate(path)
        firebase_admin.initialize_app(cred)
    else:
        # Default credentials (e.g. GOOGLE_APPLICATION_CREDENTIALS set to path)
        firebase_admin.initialize_app()
    _firebase_initialized = True
    return firebase_admin.get_app()


class SendPushRequest(BaseModel):
    title: str = Field(..., min_length=1, max_length=200, description="알림 제목")
    body: str = Field(..., max_length=1000, description="알림 본문")
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
def send_push(req: SendPushRequest) -> SendPushResponse:
    get_firebase_app()
    db = firestore.client()

    # Query fcmTokens; optionally filter by deviceName
    col_ref = db.collection(FCM_TOKENS_COLLECTION)
    if req.device_name and req.device_name.strip():
        query = col_ref.where("deviceName", "==", req.device_name.strip()).limit(
            MAX_TOKENS_PER_REQUEST
        )
    else:
        query = col_ref.limit(MAX_TOKENS_PER_REQUEST)

    docs = query.stream()
    tokens: list[str] = []
    for doc in docs:
        data = doc.to_dict()
        if isinstance(data.get("token"), str) and data["token"].strip():
            tokens.append(data["token"].strip())

    if not tokens:
        return SendPushResponse(
            success_count=0,
            failure_count=0,
            total=0,
            message="발송할 FCM 토큰이 없습니다. deviceName을 확인하거나, 앱에서 먼저 알림 권한 및 토큰 등록을 해 주세요.",
        )

    success = 0
    failure = 0
    for token in tokens:
        try:
            message = messaging.Message(
                notification=messaging.Notification(
                    title=req.title[:200],
                    body=req.body[:1000],
                ),
                token=token,
            )
            messaging.send(message)
            success += 1
        except Exception:
            failure += 1

    return SendPushResponse(
        success_count=success,
        failure_count=failure,
        total=len(tokens),
        message=f"발송 완료: 성공 {success}, 실패 {failure} (총 {len(tokens)}개 기기)",
    )
