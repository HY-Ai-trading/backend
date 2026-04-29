# 키움 AI 자동매매 서버 API

베이스 URL: `http://localhost:8000` (라즈베리파이 로컬)

## 서버 실행

```bash
cd ~/trading-server
uvicorn main:app --host 0.0.0.0 --port 8000
# 또는
python main.py
```

Swagger UI: `http://localhost:8000/docs`

## .env 주요 설정

```env
DEMO_MODE=true                  # true=모의투자, false=실전
SIGNAL_SECRET_KEY=...           # 서명 키
KIWOOM_APPKEY=...
KIWOOM_SECRET=...
KIWOOM_ACCOUNT_NO=...           # 계좌번호 (8자리)
KIWOOM_ACCOUNT_PRODUCT_CODE=01
ALLOWED_IPS=127.0.0.1           # 신호 허용 IP (미설정 시 전체 차단)
SERVER_PORT=8000
```

---

## 주문 흐름

```
1. 현재가 조회   GET /kiwoom/quote/{stock_code}
       ↓
2. 신호 전송     POST /signal/receive
       ↓
3. 결과 확인     GET /kiwoom/account
```

---

## Signal API

### POST /signal/receive — 매수/매도 신호 전송

ALLOWED_IPS에 등록된 IP만 허용.

```bash
curl -X POST http://localhost:8000/signal/receive \
  -H "Content-Type: application/json" \
  -H "x-signal-signature: {HMAC-SHA256}" \
  -d '{
    "stock_code": "005930",
    "stock_name": "삼성전자",
    "action": "BUY",
    "confidence": 0.85,
    "reason": "RSI 과매도 + 거래량 급증",
    "quantity": 2,
    "target_price": 223500
  }'
```

또는 `send_samsung.sh` 사용:
```bash
sh ~/trading-server/send_samsung.sh BUY 1 "매수 이유"
```

**요청 필드**

| 필드 | 필수 | 설명 |
|------|------|------|
| `stock_code` | ✅ | 6자리 숫자 종목코드 |
| `stock_name` | ✅ | 종목명 |
| `action` | ✅ | `BUY` 또는 `SELL` |
| `confidence` | ✅ | 신뢰도 0.0~1.0 (0.6 미만 자동 거부) |
| `reason` | ✅ | 매매 근거 |
| `quantity` | ✅ | 주문 수량 |
| `target_price` | ❌ | 지정가 단가 (미지정 시 현재 호가 자동 사용) |
| `order_type` | ❌ | `LIMIT`(기본) 또는 `MARKET` |

**order_type 동작**

| order_type | target_price | 동작 |
|---|---|---|
| `MARKET` | 무관 | 시장가 즉시 체결 |
| `LIMIT` | 지정 | 해당 가격으로 지정가 주문 |
| `LIMIT` | 미지정 | 현재 호가 자동 조회 후 지정가 주문 |

**응답**
```json
{"accepted": true, "signal_id": "uuid", "message": "매수 완료: 삼성전자 2주 @ 223,500원"}
```

**서명 생성**
```bash
SECRET="your_secret_key"
BODY='{"stock_code":"005930",...}'
SIG=$(printf '%s' "$BODY" | openssl dgst -sha256 -hmac "$SECRET" | awk '{print $2}')
```

### GET /signal/list — 수신 신호 목록

```bash
curl "http://localhost:8000/signal/list?limit=50"
```

---

## Kiwoom API

### GET /kiwoom/account — 계좌 잔고 + 보유종목

```bash
curl http://localhost:8000/kiwoom/account
```

```json
{
  "cash": 1031637,
  "total_cost": 3309500,
  "total_eval": 3371250,
  "total_profit": 31637,
  "profit_rate": 0.96,
  "holdings": [
    {
      "stock_code": "005930",
      "stock_name": "삼성전자",
      "quantity": 15,
      "avg_price": 220633,
      "current_price": 224750,
      "eval_amount": 3371250,
      "profit": 31639,
      "profit_rate": 0.96
    }
  ]
}
```

### GET /kiwoom/quote/{stock_code} — 현재 시세 (호가)

```bash
curl http://localhost:8000/kiwoom/quote/005930
```

| 응답 필드 | 설명 |
|-----------|------|
| `sel_fpr_bid` | 매도 1호가 (BUY 시 이 가격에 매수됨) |
| `buy_fpr_bid` | 매수 1호가 (SELL 시 이 가격에 매도됨) |

> 장 전/후에는 호가가 0 → 주문 거부됨. `target_price` 직접 지정하거나 `order_type=MARKET` 사용.

### GET /kiwoom/orders/filled — 당일 체결 내역

```bash
curl http://localhost:8000/kiwoom/orders/filled
```

| 응답 필드 | 설명 |
|-----------|------|
| `cntr[].ord_no` | 주문번호 |
| `cntr[].ord_stt` | `체결` = 완전체결 |
| `cntr[].cntr_pric` | 체결단가 |
| `cntr[].cntr_qty` | 체결수량 |

### GET /kiwoom/orders/unfilled — 당일 미체결 내역

```bash
curl http://localhost:8000/kiwoom/orders/unfilled
```

| 응답 필드 | 설명 |
|-----------|------|
| `oso[].ord_no` | 주문번호 |
| `oso[].stk_cd` | 종목코드 |
| `oso[].ord_pric` | 주문단가 |
| `oso[].oso_qty` | 미체결수량 |

### POST /kiwoom/cancel — 주문 취소

```bash
curl -X POST http://localhost:8000/kiwoom/cancel \
  -H "Content-Type: application/json" \
  -d '{"stock_code": "005930", "order_id": "0002454", "quantity": "0"}'
```

`quantity: "0"` = 잔량 전부 취소. RC4033 = 이미 체결됐거나 취소 불가.

### POST /kiwoom/modify — 주문 정정

```bash
curl -X POST http://localhost:8000/kiwoom/modify \
  -H "Content-Type: application/json" \
  -d '{"stock_code": "005930", "order_id": "0002454", "quantity": "2", "price": "220000", "cond_price": ""}'
```

### POST /kiwoom/sync-orders — DB 체결 동기화

PENDING 상태의 주문을 키움에서 조회해 DONE으로 업데이트.

```bash
curl -X POST http://localhost:8000/kiwoom/sync-orders
```

### GET /kiwoom/ranking — 호가잔량 상위 순위

```bash
curl "http://localhost:8000/kiwoom/ranking?mrkt_tp=001&sort_tp=1"
# mrkt_tp: 001=코스피, 101=코스닥
# sort_tp: 1=순매수잔량, 2=순매도잔량, 3=매수비율, 4=매도비율
```

### GET /kiwoom/realtime-rank — 실시간 종목 조회 순위

```bash
curl "http://localhost:8000/kiwoom/realtime-rank?qry_tp=4"
# qry_tp: 1=1분, 2=10분, 3=1시간, 4=당일누적, 5=30초
```

### WS /kiwoom/realtime — 실시간 시세 WebSocket

```
ws://localhost:8000/kiwoom/realtime?stocks=005930,000660
```

수신 이벤트:
```json
{"event": "connected", "stocks": ["005930", "000660"]}
{"trnm": "0A", "data": {...}}
```

구독 종목 변경 (클라이언트 → 서버):
```json
{"cmd": "subscribe", "stocks": ["005930", "005380"]}
```

---

## Dashboard API

### GET /dashboard/summary — 오늘 요약

```bash
curl http://localhost:8000/dashboard/summary
```

```json
{"today": "2026-04-27", "today_pnl": 0, "today_trades": 0, "total_pnl": 0, "weekly_signals": 3}
```

### GET /dashboard/trades — 거래 기록

```bash
curl "http://localhost:8000/dashboard/trades?limit=100"
```

### GET /dashboard/positions — 보유 포지션 (DB 기반)

```bash
curl http://localhost:8000/dashboard/positions
```

### GET /dashboard/account — 계좌 요약 (DB 기반)

```bash
curl http://localhost:8000/dashboard/account
```

### GET /dashboard/pnl-chart — 손익 차트 데이터

```bash
curl "http://localhost:8000/dashboard/pnl-chart?days=30"
```

---

## 리스크 체크 (서버 자동 적용)

| 조건 | 처리 |
|------|------|
| `confidence < 0.6` | 자동 거부 |
| `action == HOLD` | 주문 없음 |
| 종목코드 6자리 숫자 아님 | 거부 |
| 현재가 조회 실패 (장 외) | 거부 (`target_price` 직접 지정 필요) |
| 중복 signal_id | 409 거부 |

## 주요 종목코드

| 종목 | 코드 |
|------|------|
| 삼성전자 | 005930 |
| SK하이닉스 | 000660 |
| 현대차 | 005380 |
| NAVER | 035420 |
| 카카오 | 035720 |
| LG에너지솔루션 | 373220 |
| 셀트리온 | 068270 |

## 모의투자 지원 여부

| API | 지원 |
|-----|------|
| kt00018 계좌평가잔고 | ✅ |
| ka10075 미체결조회 | ✅ |
| ka10076 체결조회 | ✅ |
| kt00005 체결잔고 | ❌ RC9000 |
