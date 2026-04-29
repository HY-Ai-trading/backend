# Trading Server

키움증권 OpenAPI 기반 자동매매 백엔드 서버 (FastAPI)

## 기술 스택

- **FastAPI** + **uvicorn** — REST API 서버
- **SQLAlchemy (async)** + **aiosqlite** — SQLite DB
- **httpx** — 키움 REST API 호출
- **websockets** — 실시간 시세 스트리밍

## 구조

```
trading-server/
├── main.py              # 앱 진입점, CORS 설정
├── database.py          # DB 모델 (TradeRecord, SignalRecord, DailySummary)
├── models.py            # Pydantic 모델 (TradingSignal)
├── auth.py              # JWT 세션 인증
├── auth_router.py       # /auth 라우터 (로그인/로그아웃/검증)
├── kiwoom_bridge.py     # 키움 API 브릿지 (토큰·주문·조회·지표)
├── ip_whitelist.py      # IP 허용 목록
└── routers/
    ├── kiwoom_router.py    # /kiwoom — 계좌·호가·주문·실시간
    ├── signal_router.py    # /signal — AI 신호 수신·목록
    └── dashboard_router.py # /dashboard — 요약·체결내역·손익차트
```

## 주요 API

| 메서드 | 경로 | 설명 |
|--------|------|------|
| POST | `/auth/login` | 대시보드 로그인 (JWT 발급) |
| GET | `/kiwoom/account` | 계좌 잔고·보유종목 |
| GET | `/kiwoom/quote/{code}` | 호가 조회 |
| GET | `/kiwoom/indicators/{code}` | RSI·이동평균·볼린저밴드 |
| POST | `/kiwoom/sync-orders` | 키움 체결내역 → DB 동기화 |
| POST | `/kiwoom/recalc-profit` | 실현손익 재계산 |
| POST | `/signal/receive` | AI 매매신호 수신 (HMAC 인증) |
| GET | `/signal/list` | 신호 목록 |
| GET | `/dashboard/summary` | 오늘 손익·거래 요약 |
| GET | `/dashboard/trades` | 체결내역 (DB) |
| GET | `/dashboard/pnl-chart` | 누적 손익 차트 데이터 |

## 설정

`.env.example`을 복사해 `.env`로 저장 후 값 입력:

```bash
cp .env.example .env
```

## 실행

```bash
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
uvicorn main:app --host 0.0.0.0 --port 5000
```

## 인증

- **대시보드**: `POST /auth/login` → JWT 토큰 → `Authorization: Bearer <token>`
- **AI 신호**: `X-Signature` HMAC-SHA256 헤더
- **서버 내부**: `X-Api-Key` 또는 `127.0.0.1` IP 자동 통과

## 주문 방식

| 조건 | 주문 유형 |
|------|-----------|
| `target_price` 지정 | 지정가 |
| `target_price` 없음 | 현재 호가 기준 지정가 |
| `order_type=MARKET` | 시장가 |
