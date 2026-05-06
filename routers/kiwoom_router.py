"""
키움증권 조회/주문취소 API 라우터
- GET    /kiwoom/account              계좌 보유 종목
- GET    /kiwoom/quote/{code}         호가(현재 시세)
- GET    /kiwoom/ranking              순위 정보 (호가잔량상위)
- GET    /kiwoom/realtime-rank        실시간 종목 조회 순위
- POST   /kiwoom/cancel               주문 취소
- WS     /kiwoom/realtime             실시간 시세 WebSocket
"""

import os, json, asyncio
import websockets
from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Query, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from pydantic import BaseModel
import kiwoom_bridge
from database import get_db, TradeRecord
from auth import require_session
from dotenv import load_dotenv

load_dotenv()

DEMO_MODE = os.getenv("DEMO_MODE", "true").lower() == "true"
WS_URL = (
    "wss://mockapi.kiwoom.com:10000/api/dostk/websocket"
    if DEMO_MODE
    else "wss://api.kiwoom.com:10000/api/dostk/websocket"
)

router = APIRouter(prefix="/kiwoom", tags=["키움API"])


class CancelRequest(BaseModel):
    stock_code: str
    order_id: str       # 취소할 원주문번호 (ord_no)
    quantity: str = "0" # 취소수량, '0' = 잔량 전부 취소


class ModifyRequest(BaseModel):
    stock_code: str
    order_id: str   # 정정할 원주문번호
    quantity: str   # 정정수량
    price: str      # 정정단가
    cond_price: str = ""  # 정정조건단가 (조건부지정가일 때만)


async def _get_token():
    token = await kiwoom_bridge.get_access_token()
    return token


@router.get("/account")
async def get_account(_=Depends(require_session)):
    """계좌평가잔고내역 (kt00018) - 예수금 + 보유종목 + 평균단가"""
    token = await _get_token()
    if not token:
        return {"error": "토큰 획득 실패"}
    raw = await kiwoom_bridge.get_account_holdings(token)
    if raw.get("return_code") != 0:
        return raw

    def n(v): return int(v) if v else 0
    def f(v): return float(v) if v else 0.0

    holdings = [
        {
            "stock_code": h["stk_cd"].lstrip("A"),
            "stock_name": h["stk_nm"],
            "quantity": n(h["rmnd_qty"]),
            "avg_price": n(h["pur_pric"]),
            "current_price": n(h["cur_prc"]),
            "prev_close": n(h["pred_close_pric"]),
            "eval_amount": n(h["evlt_amt"]),
            "profit": n(h["evltv_prft"]),
            "profit_rate": f(h["prft_rt"]),
        }
        for h in raw.get("acnt_evlt_remn_indv_tot", [])
        if n(h["rmnd_qty"]) > 0
    ]
    total_asset = n(raw["prsm_dpst_aset_amt"])
    total_eval  = n(raw["tot_evlt_amt"])
    return {
        "cash": total_asset - total_eval,   # 순수 예수금 (현금)
        "total_asset": total_asset,          # 총 자산 (현금 + 보유종목)
        "total_cost": n(raw["tot_pur_amt"]),
        "total_eval": total_eval,
        "total_profit": n(raw["tot_evlt_pl"]),
        "profit_rate": f(raw["tot_prft_rt"]),
        "holdings": holdings,
    }


@router.get("/orders/filled")
async def get_filled_orders(_=Depends(require_session)):
    """당일 체결 내역 조회 (ka10076)"""
    token = await _get_token()
    if not token:
        return {"error": "토큰 획득 실패"}
    return await kiwoom_bridge.get_filled_orders(token)


@router.get("/orders/unfilled")
async def get_unfilled_orders(_=Depends(require_session)):
    """당일 미체결 조회 (ka10075)"""
    token = await _get_token()
    if not token:
        return {"error": "토큰 획득 실패"}
    return await kiwoom_bridge.get_unfilled_orders(token)


@router.post("/sync-orders")
async def sync_orders(db: AsyncSession = Depends(get_db), _=Depends(require_session)):
    """키움 당일 체결 내역을 DB에 동기화
    - 이미 있는 레코드(order_id 기준): PENDING→DONE 상태 업데이트
    - 없는 레코드: 신규 INSERT (키움에서 직접 낸 주문 포함)
    """
    token = await _get_token()
    if not token:
        return {"error": "토큰 획득 실패"}
    data = await kiwoom_bridge.get_filled_orders(token)
    filled = data.get("cntr", [])
    if not filled:
        return {"synced": 0, "inserted": 0, "message": "체결 내역 없음"}

    from datetime import datetime, date as _date

    def _parse_order(order: dict) -> dict:
        io_tp  = order.get("io_tp_nm", "")
        stk_cd = (order.get("stk_cd") or "").lstrip("A")
        stk_nm = (order.get("stk_nm") or "").strip()
        # 종목명이 비어있거나 코드와 같으면 빈 문자열로 → DB에서 기존값 유지
        if not stk_nm or stk_nm == stk_cd:
            stk_nm = ""
        ord_tm = order.get("ord_tm", "000000")
        h, m, s = ord_tm[:2], ord_tm[2:4], ord_tm[4:6]
        today  = _date.today()
        return {
            "ord_no":  order.get("ord_no", ""),
            "action":  "BUY" if "매수" in io_tp else "SELL",
            "stk_cd":  stk_cd,
            "stk_nm":  stk_nm,
            "price":   int(order.get("cntr_pric") or 0),
            "qty":     int(order.get("cntr_qty")  or 0),
            "cmsn":    int(order.get("tdy_trde_cmsn") or 0),
            "tax":     int(order.get("tdy_trde_tax")  or 0),
            "trade_dt": datetime(today.year, today.month, today.day,
                                 int(h), int(m), int(s)),
        }

    async def _calc_sell_profit(stk_cd: str, sell_price: int, sell_qty: int,
                                sell_cmsn: int, sell_tax: int) -> int:
        """평균 매입가 기준 실현손익 (수수료·세금·매수수수료 비례 차감)"""
        buy_res = await db.execute(
            select(TradeRecord).where(
                TradeRecord.stock_code == stk_cd,
                TradeRecord.action == "BUY",
                TradeRecord.status == "DONE",
            )
        )
        buys = buy_res.scalars().all()
        if not buys:
            return 0
        total_cost  = sum(b.price * b.quantity for b in buys)
        total_qty   = sum(b.quantity for b in buys)
        avg_buy     = total_cost / total_qty if total_qty else sell_price
        # 매수 수수료는 매도 수량 비율만큼만 차감
        ratio        = min(sell_qty / total_qty, 1.0) if total_qty else 1.0
        buy_cmsn_pro = int(sum(b.commission for b in buys) * ratio)
        gross        = int((sell_price - avg_buy) * sell_qty)
        return gross - sell_cmsn - sell_tax - buy_cmsn_pro

    orders = [_parse_order(o) for o in filled]
    # ── 1패스: BUY 먼저 DONE 처리 (SELL profit 계산 시 BUY가 DONE이어야 함) ──
    updated  = 0
    inserted = 0
    for o in [x for x in orders if x["action"] == "BUY"] + \
             [x for x in orders if x["action"] == "SELL"]:
        res    = await db.execute(select(TradeRecord).where(TradeRecord.order_id == o["ord_no"]))
        record = res.scalar_one_or_none()

        if record:
            if record.status == "PENDING":
                record.status     = "DONE"
                record.price      = o["price"]
                record.amount     = o["price"] * o["qty"]
                record.commission = o["cmsn"] + o["tax"]
                # 종목명 보정: DB에 코드나 템플릿이 저장된 경우 Kiwoom 값으로 덮어쓰기
                if o["stk_nm"] and (
                    not record.stock_name
                    or record.stock_name == record.stock_code
                    or record.stock_name == "종목명"
                ):
                    record.stock_name = o["stk_nm"]
                if o["action"] == "SELL":
                    record.profit = await _calc_sell_profit(
                        o["stk_cd"], o["price"], o["qty"], o["cmsn"], o["tax"]
                    )
                updated += 1
        else:
            profit = 0
            if o["action"] == "SELL":
                profit = await _calc_sell_profit(
                    o["stk_cd"], o["price"], o["qty"], o["cmsn"], o["tax"]
                )
            db.add(TradeRecord(
                created_at = o["trade_dt"],
                stock_code = o["stk_cd"],
                stock_name = o["stk_nm"] or o["stk_cd"],
                action     = o["action"],
                quantity   = o["qty"],
                price      = o["price"],
                amount     = o["price"] * o["qty"],
                status     = "DONE",
                order_id   = o["ord_no"],
                reason     = "키움 직접 체결 (sync)",
                commission = o["cmsn"] + o["tax"],
                profit     = profit,
            ))
            inserted += 1

    await db.commit()
    return {"updated": updated, "inserted": inserted, "total_filled": len(filled)}


@router.post("/recalc-profit")
async def recalc_profit(db: AsyncSession = Depends(get_db), _=Depends(require_session)):
    """이동평균법으로 종목별 재고 추적하며 실현손익 재계산
    - 오늘 체결내역에서 commission 업데이트
    - 시간순 BUY/SELL 처리: 전량 매도 시 avg_cost 초기화
    """
    token = await _get_token()
    if token:
        filled_data = await kiwoom_bridge.get_filled_orders(token)
        for order in filled_data.get("cntr", []):
            ord_no = order.get("ord_no", "")
            cmsn   = int(order.get("tdy_trde_cmsn") or 0)
            tax    = int(order.get("tdy_trde_tax")  or 0)
            res    = await db.execute(select(TradeRecord).where(TradeRecord.order_id == ord_no))
            rec    = res.scalar_one_or_none()
            if rec:
                rec.commission = cmsn + tax
        await db.commit()

    # 전체 DONE 체결내역을 시간순으로 읽어 종목별 이동평균 재고 추적
    # 같은 시각이면 SELL 먼저 처리 (기존 포지션 청산 후 신규 매수)
    res = await db.execute(
        select(TradeRecord)
        .where(TradeRecord.status == "DONE")
        .order_by(TradeRecord.created_at, TradeRecord.action.desc())
    )
    trades = res.scalars().all()

    # 종목별 재고: avg_cost, qty, accumulated buy commission
    inventory: dict[str, dict] = {}
    fixed = 0

    for t in trades:
        code = t.stock_code
        if code not in inventory:
            inventory[code] = {"avg_cost": 0.0, "qty": 0, "buy_cmsn": 0.0}

        iv = inventory[code]

        if t.action == "BUY":
            total       = iv["avg_cost"] * iv["qty"] + t.price * t.quantity
            iv["qty"]      += t.quantity
            iv["avg_cost"]  = total / iv["qty"] if iv["qty"] else t.price
            iv["buy_cmsn"] += t.commission

        elif t.action == "SELL":
            avg  = iv["avg_cost"] if iv["qty"] > 0 else t.price
            # 매수 수수료는 매도 수량 비율만큼 차감
            ratio        = min(t.quantity / iv["qty"], 1.0) if iv["qty"] > 0 else 0
            buy_cmsn_cut = int(iv["buy_cmsn"] * ratio)
            gross        = int((t.price - avg) * t.quantity)
            t.profit     = gross - t.commission - buy_cmsn_cut

            iv["qty"]      = max(0, iv["qty"] - t.quantity)
            iv["buy_cmsn"] = max(0.0, iv["buy_cmsn"] - buy_cmsn_cut)
            if iv["qty"] == 0:
                iv["avg_cost"]  = 0.0
                iv["buy_cmsn"]  = 0.0
            fixed += 1

    await db.commit()
    return {"recalculated": fixed}


@router.get("/indicators/{stock_code}")
async def get_indicators(stock_code: str, _=Depends(require_session)):
    """기술적 지표 조회 — RSI·이동평균·볼린저밴드·거래량비율 (ka10080 일봉 기반)"""
    token = await _get_token()
    if not token:
        return {"error": "토큰 획득 실패"}
    ohlcv = await kiwoom_bridge.get_daily_ohlcv(token, stock_code, count=30)
    if not ohlcv:
        return {"error": "일봉 데이터 없음", "code": stock_code}
    indicators = kiwoom_bridge.calc_indicators(ohlcv)
    return {"code": stock_code, **indicators}


@router.get("/quote/{stock_code}")
async def get_quote(stock_code: str, _=Depends(require_session)):
    """주식 호가 조회 (ka10004) - 매수/매도 1~10호가, 현재가"""
    token = await _get_token()
    if not token:
        return {"error": "토큰 획득 실패"}
    return await kiwoom_bridge.get_stock_quote(token, stock_code)


@router.get("/ranking")
async def get_ranking(
    mrkt_tp: str = Query(default="001", description="001=코스피, 101=코스닥"),
    sort_tp: str = Query(default="1", description="1=순매수잔량, 2=순매도잔량, 3=매수비율, 4=매도비율"),
    stex_tp: str = Query(default="1", description="1=KRX, 2=NXT, 3=통합"),
):
    """호가잔량상위 순위 조회 (ka10020)"""
    token = await _get_token()
    if not token:
        return {"error": "토큰 획득 실패"}
    return await kiwoom_bridge.get_stock_ranking(token, mrkt_tp, sort_tp, stex_tp)


@router.post("/cancel")
async def cancel_order(req: CancelRequest):
    """주문 취소 kt10003 - 미체결 주문 취소 (quantity='0' 이면 전량)"""
    return await kiwoom_bridge.cancel_order(
        stock_code=req.stock_code,
        orig_ord_no=req.order_id,
        cncl_qty=req.quantity,
    )


@router.post("/modify")
async def modify_order(req: ModifyRequest):
    """주문 정정 kt10002 - 미체결 주문의 가격/수량 변경"""
    return await kiwoom_bridge.modify_order(
        stock_code=req.stock_code,
        orig_ord_no=req.order_id,
        mdfy_qty=req.quantity,
        mdfy_uv=req.price,
        mdfy_cond_uv=req.cond_price,
    )


@router.get("/realtime-rank")
async def get_realtime_rank(
    qry_tp: str = Query(default="4", description="1=1분, 2=10분, 3=1시간, 4=당일누적, 5=30초"),
):
    """실시간 종목 조회 순위 (ka00198)"""
    token = await _get_token()
    if not token:
        return {"error": "토큰 획득 실패"}
    return await kiwoom_bridge.get_realtime_stock_ranking(token, qry_tp)


@router.websocket("/realtime")
async def websocket_realtime(
    websocket: WebSocket,
    stocks: str = Query(default="005930", description="콤마로 구분된 종목코드 (예: 005930,000660)"),
):
    """실시간 시세 WebSocket 스트리밍

    연결 후 수신 메시지 형식:
    - {"event": "connected", "stocks": [...]}
    - {"trnm": "...", ...}  키움 실시간 데이터

    클라이언트 → 서버 메시지:
    - {"cmd": "subscribe", "stocks": ["005930", "000660"]}  구독 변경
    """
    await websocket.accept()

    token = await _get_token()
    if not token:
        await websocket.send_json({"error": "토큰 획득 실패"})
        await websocket.close()
        return

    stock_list = [s.strip() for s in stocks.split(",") if s.strip()]

    try:
        async with websockets.connect(WS_URL) as kw:
            # 1. 로그인
            await kw.send(json.dumps({"trnm": "LOGIN", "token": token}))
            login_resp = json.loads(await kw.recv())

            if login_resp.get("return_code") != 0:
                await websocket.send_json({
                    "error": f"키움 WS 로그인 실패: {login_resp.get('return_msg')}"
                })
                return

            await websocket.send_json({"event": "connected", "stocks": stock_list})

            # 2. 실시간 종목 등록
            # 0A=주식현재가, 0B=주식체결, 0g=주식종목정보(현재가·등락)
            await kw.send(json.dumps({
                "trnm": "REG",
                "grp_no": "1",
                "refresh": "1",
                "data": [{"item": stock_list, "type": ["0A", "0B", "0g"]}],
            }))

            async def from_kiwoom():
                async for raw in kw:
                    data = json.loads(raw)
                    if data.get("trnm") == "PING":
                        await kw.send(raw)  # PONG
                        continue
                    try:
                        await websocket.send_json(data)
                    except Exception:
                        break

            async def from_client():
                while True:
                    try:
                        msg = await websocket.receive_json()
                        if msg.get("cmd") == "subscribe":
                            new_stocks = msg.get("stocks", [])
                            await kw.send(json.dumps({
                                "trnm": "REG",
                                "grp_no": "1",
                                "refresh": "0",
                                "data": [{"item": new_stocks, "type": ["0A", "0B", "0g"]}],
                            }))
                    except Exception:
                        break

            await asyncio.gather(from_kiwoom(), from_client())

    except WebSocketDisconnect:
        pass
    except Exception as e:
        try:
            await websocket.send_json({"error": str(e)})
        except Exception:
            pass
