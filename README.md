# Push PWA Backend

프론트에서 알림 내용(제목·본문)을 받아 Firestore에 저장된 FCM 토큰으로 푸시를 발송하는 FastAPI 서버입니다.

## 설정

1. Firebase Console에서 서비스 계정 JSON 키를 생성합니다.
2. `.env` 또는 환경 변수로 키 경로를 지정합니다.
   - `GOOGLE_APPLICATION_CREDENTIALS=path/to/serviceAccountKey.json`
   - 또는 `FIREBASE_SERVICE_ACCOUNT_PATH=path/to/serviceAccountKey.json`
3. `cp .env.example .env` 후 실제 경로로 수정합니다.

## 설치 및 실행

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate
# macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
uvicorn main:app --reload --host 0.0.0.0 --port 8000
```

## API

- `GET /health` — 서버 상태
- `POST /api/send-push` — 푸시 발송
  - Body: `{ "title": "제목", "body": "본문", "deviceName": "선택" }`
  - `deviceName` 생략 시 Firestore `fcmTokens` 전체로 발송

## ngrok 연동

로컬에서 BE 실행 후 다른 터미널에서:

```bash
ngrok http 8000
```

출력된 HTTPS URL을 프론트 환경 변수 `VITE_PUSH_API_URL`에 넣고 Vercel 프로젝트를 재배포하면, 배포된 PWA에서 푸시 보내기를 테스트할 수 있습니다.
