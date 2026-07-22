"""
키움증권 API 브릿지
공식 API 문서: https://openapi.kiwoom.com/guide/apiguide
- 모의투자 도메인: https://mockapi.kiwoom.com
- 실전투자 도메인: https://api.kiwoom.com
"""

import os
import asyncio
import httpx
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from database import TradeRecord, SignalRecord, LockedStock
from models import TradingSignal
from dotenv import load_dotenv

KST = ZoneInfo("Asia/Seoul")

load_dotenv()

DEMO_MODE = os.getenv("DEMO_MODE", "true").lower() == "true"
KIWOOM_APPKEY = os.getenv("KIWOOM_APPKEY", "")
KIWOOM_SECRET = os.getenv("KIWOOM_SECRET", "")
KIWOOM_ACCOUNT_NO = os.getenv("KIWOOM_ACCOUNT_NO", "")
KIWOOM_API_URL = "https://mockapi.kiwoom.com" if DEMO_MODE else "https://api.kiwoom.com"

DEMO_STARTING_BALANCE = float(os.getenv("DEMO_STARTING_BALANCE", "10000000"))
DEMO_COMMISSION_RATE = float(os.getenv("DEMO_COMMISSION_RATE", "0.0005"))

_token_cache: dict = {"access_token": None, "expires_dt": None}

demo_account = {
    "balance": DEMO_STARTING_BALANCE,
    "holdings": {},
}


def _kiwoom_headers(token: str, api_id: str) -> dict:
    return {
        "Content-Type": "application/json;charset=UTF-8",
        "authorization": f"Bearer {token}",
        "cont-yn": "N",
        "next-key": "",
        "api-id": api_id,
    }


def _now_kst() -> datetime:
    return datetime.now(tz=KST)


def _fmt_dt(raw: str) -> str:
    """'20260428080213' → '2026-04-28 08:02:13'"""
    try:
        return datetime.strptime(raw, "%Y%m%d%H%M%S").strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return raw


def _is_token_valid() -> bool:
    if not _token_cache["access_token"] or not _token_cache["expires_dt"]:
        return False
    try:
        expires = datetime.strptime(_token_cache["expires_dt"], "%Y%m%d%H%M%S").replace(tzinfo=KST)
        # 만료 5분 전부터 재발급
        return _now_kst() < expires - timedelta(minutes=5)
    except Exception:
        return False


async def get_access_token() -> str | None:
    if _is_token_valid():
        print(f"✅ 토큰 재사용 | 만료: {_fmt_dt(_token_cache['expires_dt'])} | 현재: {_now_kst().strftime('%Y-%m-%d %H:%M:%S')}")
        return _token_cache["access_token"]

    # 만료됐으면 캐시 초기화 후 재발급
    _token_cache["access_token"] = None
    _token_cache["expires_dt"] = None

    if not KIWOOM_APPKEY or not KIWOOM_SECRET:
        print("⚠️  KIWOOM_APPKEY, KIWOOM_SECRET이 설정되지 않았습니다.")
        return None

    print(f"🔄 토큰 재발급 | 현재: {_now_kst().strftime('%Y-%m-%d %H:%M:%S')}")
    async with httpx.AsyncClient() as client:
        try:
            response = await client.post(
                f"{KIWOOM_API_URL}/oauth2/token",
                headers={"Content-Type": "application/json;charset=UTF-8"},
                json={
                    "grant_type": "client_credentials",
                    "appkey": KIWOOM_APPKEY,
                    "secretkey": KIWOOM_SECRET,
                },
                timeout=10,
            )
            data = response.json()
            if data.get("return_code") != 0:
                print(f"❌ 토큰 획득 실패: {data.get('return_msg', '알 수 없는 오류')}")
                return None

            _token_cache["access_token"] = data["token"]
            _token_cache["expires_dt"] = data["expires_dt"]
            print(f"✅ 토큰 발급 | 만료: {_fmt_dt(data['expires_dt'])} KST")
            return data["token"]
        except Exception as e:
            print(f"❌ 토큰 획득 중 오류: {e}")
            return None


def _parse_price(raw: str) -> int:
    """'+223500', '-5000', '223500' 형태를 int로 변환"""
    try:
        return abs(int(str(raw).replace(",", "").replace("+", "").strip()))
    except Exception:
        return 0


async def get_stock_quote_prices(token: str, stock_code: str) -> dict:
    """주식호가 조회 (ka10004) → 매수/매도 1호가 반환
    Returns: {"ask": int, "bid": int}  (ask=매도1호가/사야할가격, bid=매수1호가/팔수있는가격)
    """
    async with httpx.AsyncClient() as client:
        try:
            response = await client.post(
                f"{KIWOOM_API_URL}/api/dostk/mrkcond",
                headers=_kiwoom_headers(token, "ka10004"),
                json={"stk_cd": stock_code},
                timeout=10,
            )
            data = response.json()
            ask = _parse_price(data.get("sel_fpr_bid", "0"))  # 매도1호가 (살 때 기준)
            bid = _parse_price(data.get("buy_fpr_bid", "0"))  # 매수1호가 (팔 때 기준)
            print(f"📊 {stock_code} 호가 - 매도1호가(ask): {ask:,} / 매수1호가(bid): {bid:,}")
            return {"ask": ask, "bid": bid, "raw": data}
        except Exception as e:
            print(f"⚠️  시세 조회 실패: {e}")
            return {"ask": 0, "bid": 0, "raw": {}}


async def get_stock_price(token: str, stock_code: str) -> int:
    """매도1호가(ask) 반환 - 하위 호환용"""
    prices = await get_stock_quote_prices(token, stock_code)
    return prices["ask"]


async def cancel_order(stock_code: str, orig_ord_no: str, cncl_qty: str = "0") -> dict:
    """주문 취소 kt10003 (매수/매도 공통)
    cncl_qty: 취소수량, '0' = 잔량 전부 취소
    """
    token = await get_access_token()
    if not token:
        return {"success": False, "message": "토큰 획득 실패"}

    body = {
        "dmst_stex_tp": "KRX",
        "stk_cd": stock_code,
        "orig_ord_no": orig_ord_no,
        "cncl_qty": cncl_qty,
    }
    print(f"📤 주문취소 요청: api-id=kt10003, body={body}")
    async with httpx.AsyncClient() as client:
        try:
            response = await client.post(
                f"{KIWOOM_API_URL}/api/dostk/ordr",
                headers=_kiwoom_headers(token, "kt10003"),
                json=body,
                timeout=10,
            )
            result = response.json()
            print(f"📡 취소 응답: {result}")
            if result.get("return_code") != 0:
                return {"success": False, "message": result.get("return_msg", "취소 실패")}
            return {"success": True, "message": result.get("return_msg", "취소 완료"), "result": result}
        except Exception as e:
            return {"success": False, "message": str(e)}


async def auto_adjust_pending_buy_orders(db: AsyncSession) -> list[dict]:
    """미체결 매수 지정가 주문 중 현재가가 내려갔으면 취소 후 현재가로 재주문.
    장 시간(09:00~15:30 KST)에만 동작.
    """
    now = _now_kst()
    market_open  = now.replace(hour=9,  minute=0,  second=0, microsecond=0)
    market_close = now.replace(hour=15, minute=30, second=0, microsecond=0)
    if not (market_open <= now <= market_close):
        return []

    token = await get_access_token()
    if not token:
        return []

    raw = await get_unfilled_orders(token)
    if raw.get("return_code") != 0:
        return []

    # 응답에서 주문 목록 찾기 (ord_no 가 있는 첫 번째 리스트)
    orders: list[dict] = []
    for val in raw.values():
        if isinstance(val, list) and val and isinstance(val[0], dict) and "ord_no" in val[0]:
            orders = val
            break
    if not orders:
        for val in raw.values():
            if isinstance(val, list) and val and isinstance(val[0], dict):
                orders = val
                break

    if not orders:
        return []

    results = []
    for order in orders:
        # 매수(1)이고 지정가("보통"/"0")인 주문만
        if str(order.get("buy_sell_tp", "")).strip() != "1":
            continue
        trde_tp_val = str(order.get("trde_tp", "")).strip()
        if trde_tp_val not in ("보통", "0", "지정가"):
            continue

        ord_no  = str(order.get("ord_no",  "")).strip()
        stk_cd  = str(order.get("stk_cd",  "")).strip()
        stk_nm  = str(order.get("stk_nm",  "")).strip()
        if not ord_no or not stk_cd:
            continue

        try:
            ord_price = _parse_price(order.get("ord_uv", "0"))
            ord_qty   = int(str(order.get("ord_qty",  "0")).replace(",", "").strip())
            cntr_qty  = int(str(order.get("cntr_qty", "0")).replace(",", "").strip())
        except (ValueError, TypeError):
            continue

        if ord_price == 0 or ord_qty == 0:
            continue

        remain_qty = ord_qty - cntr_qty
        if remain_qty <= 0:
            continue

        # 현재 매도1호가 조회
        prices  = await get_stock_quote_prices(token, stk_cd)
        cur_ask = prices["ask"]
        if cur_ask == 0:
            continue

        # 가격이 10원 이상 내려갔을 때만 조정
        if cur_ask >= ord_price or (ord_price - cur_ask) < 10:
            continue

        print(f"📉 {stk_nm}({stk_cd}) 주문가 {ord_price:,}원 → 현재가 {cur_ask:,}원 (하락) → 취소 후 재주문")

        # 기존 주문 취소
        cancel_res = await cancel_order(stk_cd, ord_no)
        if not cancel_res.get("success"):
            print(f"⚠️  취소 실패: {cancel_res.get('message')}")
            results.append({"action": "cancel_failed", "stk_cd": stk_cd, "ord_no": ord_no})
            continue

        await asyncio.sleep(0.5)  # 취소 반영 대기

        # 현재 매도1호가로 재매수 주문
        order_body = {
            "dmst_stex_tp": "KRX",
            "stk_cd":  stk_cd,
            "ord_qty": str(remain_qty),
            "ord_uv":  str(cur_ask),
            "trde_tp": "0",
            "cond_uv": "",
        }
        async with httpx.AsyncClient() as client:
            try:
                resp = await client.post(
                    f"{KIWOOM_API_URL}/api/dostk/ordr",
                    headers=_kiwoom_headers(token, "kt10000"),
                    json=order_body,
                    timeout=10,
                )
                buy_res = resp.json()
            except Exception as e:
                print(f"⚠️  재주문 실패: {e}")
                results.append({"action": "rebuy_failed", "stk_cd": stk_cd})
                continue

        if buy_res.get("return_code") != 0:
            print(f"⚠️  재주문 API 오류: {buy_res.get('return_msg')}")
            results.append({"action": "rebuy_api_error", "stk_cd": stk_cd, "msg": buy_res.get("return_msg")})
            continue

        new_ord_no = buy_res.get("ord_no", "")
        print(f"✅ {stk_nm} 재주문 완료: {ord_no} → {new_ord_no}, {cur_ask:,}원 × {remain_qty}주")

        # DB: 기존 PENDING TradeRecord의 주문번호·가격 갱신
        result = await db.execute(
            select(TradeRecord).where(
                TradeRecord.order_id == ord_no,
                TradeRecord.status == "PENDING",
            )
        )
        trade = result.scalar_one_or_none()
        if trade:
            trade.order_id = new_ord_no
            trade.price    = cur_ask
            trade.amount   = cur_ask * remain_qty
            await db.commit()
            print(f"✅ DB 업데이트: trade_id={trade.id}, {ord_no} → {new_ord_no}")

        results.append({
            "action":    "adjusted",
            "stk_cd":   stk_cd,
            "stk_nm":   stk_nm,
            "old_ord_no": ord_no,
            "new_ord_no": new_ord_no,
            "old_price":  ord_price,
            "new_price":  cur_ask,
            "quantity":   remain_qty,
        })

    return results


async def modify_order(
    stock_code: str,
    orig_ord_no: str,
    mdfy_qty: str,
    mdfy_uv: str,
    mdfy_cond_uv: str = "",
) -> dict:
    """주문 정정 kt10002 - 미체결 주문의 수량/가격 변경
    mdfy_qty: 정정수량
    mdfy_uv:  정정단가
    """
    token = await get_access_token()
    if not token:
        return {"success": False, "message": "토큰 획득 실패"}

    body = {
        "dmst_stex_tp": "KRX",
        "orig_ord_no": orig_ord_no,
        "stk_cd": stock_code,
        "mdfy_qty": mdfy_qty,
        "mdfy_uv": mdfy_uv,
        "mdfy_cond_uv": mdfy_cond_uv,
    }
    print(f"📤 주문정정 요청: api-id=kt10002, body={body}")
    async with httpx.AsyncClient() as client:
        try:
            response = await client.post(
                f"{KIWOOM_API_URL}/api/dostk/ordr",
                headers=_kiwoom_headers(token, "kt10002"),
                json=body,
                timeout=10,
            )
            result = response.json()
            print(f"📡 정정 응답: {result}")
            if result.get("return_code") != 0:
                return {"success": False, "message": result.get("return_msg", "정정 실패")}
            return {"success": True, "message": result.get("return_msg", "정정 완료"), "result": result}
        except Exception as e:
            return {"success": False, "message": str(e)}


async def send_order(signal: TradingSignal, db: AsyncSession) -> dict:
    try:
        quantity = signal.quantity or 1

        if signal.action == "SELL" and await db.get(LockedStock, signal.stock_code):
            msg = f"{signal.stock_name or signal.stock_code}은 매도 잠금 상태입니다"
            print(f"🔒 {msg}")
            return {"success": False, "message": msg, "trade_id": None, "order_id": None}

        if not KIWOOM_APPKEY or not KIWOOM_ACCOUNT_NO:
            return {"success": False, "message": "API 인증정보 미설정", "trade_id": None, "order_id": None}

        token = await get_access_token()
        if not token:
            return {"success": False, "message": "토큰 획득 실패", "trade_id": None, "order_id": None}

        # ── 1. 주문 유형 결정 ──
        is_market = (signal.order_type or "").upper() == "MARKET"

        if is_market:
            # 시장가: 즉시 체결, 가격 미지정
            order_price = 0
            trde_tp = "3"
            ord_uv = ""
            print(f"📌 시장가 주문")
        elif signal.target_price:
            # 명시적 지정가
            order_price = signal.target_price
            trde_tp = "0"
            ord_uv = str(order_price)
            print(f"📌 지정가 사용: {order_price:,}원")
        else:
            # 현재 호가 조회 → 지정가 주문
            prices = await get_stock_quote_prices(token, signal.stock_code)
            order_price = prices["ask"] if signal.action == "BUY" else prices["bid"]
            if order_price == 0:
                return {
                    "success": False,
                    "message": "현재가 조회 실패 (장 전이거나 시세 없음). order_type=MARKET 또는 target_price를 직접 지정하세요.",
                    "trade_id": None,
                    "order_id": None,
                }
            trde_tp = "0"
            ord_uv = str(order_price)
            print(f"📌 현재가 기준 지정가: {order_price:,}원")

        current_price = order_price

        # ── 2. 주문 ──
        api_id = "kt10000" if signal.action == "BUY" else "kt10001"
        order_body = {
            "dmst_stex_tp": "KRX",
            "stk_cd": signal.stock_code,
            "ord_qty": str(quantity),
            "ord_uv": ord_uv,
            "trde_tp": trde_tp,
            "cond_uv": "",
        }

        print(f"📤 주문 요청: {KIWOOM_API_URL}/api/dostk/ordr")
        print(f"📤 api-id={api_id}, body={order_body}")

        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{KIWOOM_API_URL}/api/dostk/ordr",
                headers=_kiwoom_headers(token, api_id),
                json=order_body,
                timeout=10,
            )
            api_result = response.json()
            print(f"📡 HTTP {response.status_code} 응답: {api_result}")

        # 성공 여부 판단 (return_code 0 = 성공)
        if api_result.get("return_code") != 0:
            error_msg = api_result.get("return_msg", "API 오류")
            print(f"❌ 주문 실패: {error_msg}")
            return {"success": False, "message": f"API 오류: {error_msg}", "trade_id": None, "order_id": None}

        order_id = api_result.get("ord_no")
        order_amount = current_price * quantity

        record = TradeRecord(
            stock_code=signal.stock_code,
            stock_name=signal.stock_name,
            action=signal.action,
            quantity=quantity,
            price=current_price,
            amount=order_amount,
            status="PENDING",
            signal_id=signal.signal_id,
            reason=signal.reason,
            profit=0,
            order_id=order_id,
        )
        db.add(record)
        await db.commit()
        await db.refresh(record)

        # 신호 상태 업데이트
        sig_rec = await db.execute(
            select(SignalRecord).where(SignalRecord.signal_id == signal.signal_id)
        )
        sig_obj = sig_rec.scalar_one_or_none()
        if sig_obj:
            sig_obj.executed = True
            await db.commit()

        msg = f"{'매수' if signal.action == 'BUY' else '매도'} 주문 접수: {signal.stock_name} {quantity}주 (주문번호: {order_id})"
        print(f"✅ {msg}")
        return {"success": True, "message": msg, "trade_id": record.id, "order_id": order_id}

    except Exception as e:
        error_msg = f"주문 실패: {str(e)}"
        print(f"❌ {error_msg}")
        return {"success": False, "message": error_msg, "trade_id": None, "order_id": None}


async def get_total_asset() -> int | None:
    """현재 계좌 총자산(예수금+평가금액) 조회. 실패 시 None"""
    token = await get_access_token()
    if not token:
        return None
    raw = await get_account_holdings(token)
    if raw.get("return_code") != 0:
        return None
    try:
        return int(raw.get("prsm_dpst_aset_amt") or 0)
    except (TypeError, ValueError):
        return None


async def get_account_holdings(token: str) -> dict:
    """계좌평가잔고내역 조회 (kt00018) - 잔고+보유종목+평균단가"""
    prdt = os.getenv("KIWOOM_ACCOUNT_PRODUCT_CODE", "01")
    async with httpx.AsyncClient() as client:
        try:
            response = await client.post(
                f"{KIWOOM_API_URL}/api/dostk/acnt",
                headers=_kiwoom_headers(token, "kt00018"),
                json={"acnt_no": KIWOOM_ACCOUNT_NO, "acnt_prdt_cd": prdt, "qry_tp": "0", "dmst_stex_tp": "KRX"},
                timeout=10,
            )
            return response.json()
        except Exception as e:
            return {"error": str(e)}


async def get_filled_orders(token: str) -> dict:
    """당일 체결 내역 조회 (ka10076)"""
    prdt = os.getenv("KIWOOM_ACCOUNT_PRODUCT_CODE", "01")
    async with httpx.AsyncClient() as client:
        try:
            response = await client.post(
                f"{KIWOOM_API_URL}/api/dostk/acnt",
                headers=_kiwoom_headers(token, "ka10076"),
                json={
                    "acnt_no": KIWOOM_ACCOUNT_NO, "acnt_prdt_cd": prdt,
                    "qry_tp": "0", "sell_tp": "0", "stex_tp": "1",
                },
                timeout=10,
            )
            return response.json()
        except Exception as e:
            return {"error": str(e)}


async def get_unfilled_orders(token: str) -> dict:
    """당일 미체결 조회 (ka10075)"""
    prdt = os.getenv("KIWOOM_ACCOUNT_PRODUCT_CODE", "01")
    async with httpx.AsyncClient() as client:
        try:
            response = await client.post(
                f"{KIWOOM_API_URL}/api/dostk/acnt",
                headers=_kiwoom_headers(token, "ka10075"),
                json={
                    "acnt_no": KIWOOM_ACCOUNT_NO, "acnt_prdt_cd": prdt,
                    "all_stk_tp": "0", "trde_tp": "0", "stex_tp": "1",
                },
                timeout=10,
            )
            return response.json()
        except Exception as e:
            return {"error": str(e)}


async def get_stock_quote(token: str, stock_code: str) -> dict:
    """주식 호가 조회 (ka10004) - 매수/매도 1~10호가"""
    async with httpx.AsyncClient() as client:
        try:
            response = await client.post(
                f"{KIWOOM_API_URL}/api/dostk/mrkcond",
                headers=_kiwoom_headers(token, "ka10004"),
                json={"stk_cd": stock_code},
                timeout=10,
            )
            return response.json()
        except Exception as e:
            return {"error": str(e)}


async def get_realtime_stock_ranking(token: str, qry_tp: str = "4") -> dict:
    """실시간 종목 조회 순위 (ka00198)
    qry_tp: 1=1분, 2=10분, 3=1시간, 4=당일누적, 5=30초
    """
    async with httpx.AsyncClient() as client:
        try:
            response = await client.post(
                f"{KIWOOM_API_URL}/api/dostk/stkinfo",
                headers=_kiwoom_headers(token, "ka00198"),
                json={"qry_tp": qry_tp},
                timeout=10,
            )
            return response.json()
        except Exception as e:
            return {"error": str(e)}


async def get_stock_ranking(
    token: str,
    mrkt_tp: str = "001",
    sort_tp: str = "1",
    stex_tp: str = "1",
) -> dict:
    """호가잔량상위 순위 조회 (ka10020)
    mrkt_tp: 001=코스피, 101=코스닥
    sort_tp: 1=순매수잔량순, 2=순매도잔량순, 3=매수비율순, 4=매도비율순
    stex_tp: 1=KRX, 2=NXT, 3=통합
    """
    async with httpx.AsyncClient() as client:
        try:
            response = await client.post(
                f"{KIWOOM_API_URL}/api/dostk/rkinfo",
                headers=_kiwoom_headers(token, "ka10020"),
                json={
                    "mrkt_tp": mrkt_tp,
                    "sort_tp": sort_tp,
                    "trde_qty_tp": "0000",
                    "stk_cnd": "0",
                    "crd_cnd": "0",
                    "stex_tp": stex_tp,
                },
                timeout=10,
            )
            return response.json()
        except Exception as e:
            return {"error": str(e)}


async def get_daily_ohlcv(token: str, stock_code: str, count: int = 30) -> list[dict]:
    """주식일봉차트 조회 (ka10081) → 최근 count일 OHLCV 리스트 반환
    각 항목: {"date": "20260428", "open": int, "high": int, "low": int, "close": int, "volume": int}
    최신 날짜가 index 0
    """
    from datetime import date as _date
    base_dt = _date.today().strftime("%Y%m%d")
    async with httpx.AsyncClient() as client:
        try:
            response = await client.post(
                f"{KIWOOM_API_URL}/api/dostk/chart",
                headers=_kiwoom_headers(token, "ka10081"),
                json={"stk_cd": stock_code, "base_dt": base_dt, "upd_stkpc_tp": "1"},
                timeout=10,
            )
            data = response.json()
        except Exception:
            return []

    if data.get("return_code") != 0:
        return []

    rows = data.get("stk_dt_pole_chart_qry", [])
    result = []
    for r in rows[:count]:
        try:
            result.append({
                "date":   r.get("dt", ""),
                "open":   _parse_price(r.get("open_pric", "0")),
                "high":   _parse_price(r.get("high_pric", "0")),
                "low":    _parse_price(r.get("low_pric", "0")),
                "close":  _parse_price(r.get("cur_prc", "0")),
                "volume": int(str(r.get("trde_qty", "0")).replace(",", "") or 0),
            })
        except Exception:
            continue
    return result


async def get_minute_ohlcv(token: str, stock_code: str, tic_scope: str = "5", count: int = 100) -> list[dict]:
    """주식분봉차트 조회 (ka10080) → 최근 count개 분봉 OHLCV 리스트 반환
    tic_scope: 1=1분, 3=3분, 5=5분, 10=10분, 15=15분, 30=30분, 45=45분, 60=60분
    각 항목: {"datetime": "20260517 093000", "open": int, "high": int, "low": int, "close": int, "volume": int}
    최신 데이터가 index 0
    """
    from datetime import date as _date
    base_dt = _date.today().strftime("%Y%m%d")
    async with httpx.AsyncClient() as client:
        try:
            response = await client.post(
                f"{KIWOOM_API_URL}/api/dostk/chart",
                headers=_kiwoom_headers(token, "ka10080"),
                json={"stk_cd": stock_code, "tic_scope": tic_scope, "upd_stkpc_tp": "1", "base_dt": base_dt},
                timeout=10,
            )
            data = response.json()
        except Exception:
            return []

    if data.get("return_code") != 0:
        return []

    rows = data.get("stk_min_pole_chart_qry", [])
    result = []
    for r in rows[:count]:
        try:
            cntr_tm = r.get("cntr_tm", "")  # "20260515153000" = YYYYMMDDHHMMSS
            result.append({
                "datetime": cntr_tm,
                "open":     _parse_price(r.get("open_pric", "0")),
                "high":     _parse_price(r.get("high_pric", "0")),
                "low":      _parse_price(r.get("low_pric", "0")),
                "close":    _parse_price(r.get("cur_prc", "0")),
                "volume":   int(str(r.get("trde_qty", "0")).replace(",", "") or 0),
            })
        except Exception:
            continue
    return result


def _calc_rsi(closes: list[float], period: int = 14) -> float | None:
    if len(closes) < period + 1:
        return None
    gains, losses = [], []
    for i in range(1, period + 1):
        diff = closes[i - 1] - closes[i]   # 최신→과거 순이므로 부호 반전
        (gains if diff > 0 else losses).append(abs(diff))
    avg_gain = sum(gains) / period if gains else 0
    avg_loss = sum(losses) / period if losses else 0
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return round(100 - 100 / (1 + rs), 2)


def calc_indicators(ohlcv: list[dict]) -> dict:
    """OHLCV 리스트(최신 index 0)로 지표 계산"""
    if not ohlcv:
        return {}

    closes  = [d["close"]  for d in ohlcv]
    volumes = [d["volume"] for d in ohlcv]

    close_today = closes[0]
    close_prev  = closes[1] if len(closes) > 1 else close_today

    change_rate = round((close_today - close_prev) / close_prev * 100, 2) if close_prev else 0

    ma5  = round(sum(closes[:5])  / min(5,  len(closes)), 0) if closes else None
    ma20 = round(sum(closes[:20]) / min(20, len(closes)), 0) if closes else None

    vol_today   = volumes[0] if volumes else 0
    vol_avg20   = sum(volumes[:20]) / min(20, len(volumes)) if volumes else 1
    volume_ratio = round(vol_today / vol_avg20, 2) if vol_avg20 else 0

    # 볼린저 밴드 (20일)
    bb_mid = ma20
    if bb_mid and len(closes) >= 20:
        std = (sum((c - bb_mid) ** 2 for c in closes[:20]) / 20) ** 0.5
        bb_upper = round(bb_mid + 2 * std, 0)
        bb_lower = round(bb_mid - 2 * std, 0)
    else:
        bb_upper = bb_lower = None

    rsi_14 = _calc_rsi(closes, 14)

    return {
        "close":        close_today,
        "change_rate":  change_rate,
        "rsi_14":       rsi_14,
        "ma_5":         int(ma5)  if ma5  else None,
        "ma_20":        int(ma20) if ma20 else None,
        "bb_upper":     int(bb_upper) if bb_upper else None,
        "bb_lower":     int(bb_lower) if bb_lower else None,
        "volume_ratio": volume_ratio,
        "volume_today": vol_today,
    }


def get_demo_account_status() -> dict:
    holdings_value = sum(
        h["quantity"] * h["avg_price"] for h in demo_account["holdings"].values()
    )
    total_assets = demo_account["balance"] + holdings_value
    total_profit = total_assets - DEMO_STARTING_BALANCE

    return {
        "balance": demo_account["balance"],
        "holdings_value": holdings_value,
        "total_assets": total_assets,
        "total_profit": total_profit,
        "profit_rate": (total_profit / DEMO_STARTING_BALANCE * 100) if DEMO_STARTING_BALANCE > 0 else 0,
        "positions": [
            {
                "stock_code": code,
                "quantity": h["quantity"],
                "avg_price": h["avg_price"],
                "total_cost": h["quantity"] * h["avg_price"],
            }
            for code, h in demo_account["holdings"].items()
        ],
        "mode": "DEMO" if DEMO_MODE else "REAL",
    }
