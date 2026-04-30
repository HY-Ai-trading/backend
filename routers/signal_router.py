import uuid, os, hmac, hashlib, asyncio
from fastapi import APIRouter, HTTPException, Header, Depends, Request
from pydantic import BaseModel, Field
from typing import Optional
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from database import get_db, AsyncSessionLocal, SignalRecord, TradeRecord
from ip_whitelist import require_allowed_ip
from auth import require_session
from dotenv import load_dotenv
from models import TradingSignal
import kiwoom_bridge

load_dotenv()

load_dotenv()

SIGNAL_SECRET = os.getenv("SIGNAL_SECRET_KEY", "changeme")

router = APIRouter(prefix="/signal", tags=["매매신호"])


async def _auto_sync_order(ord_no: str, stk_cd: str):
    """주문 접수 후 최대 60초 동안 5초마다 체결 확인 → DB 자동 업데이트"""
    for attempt in range(12):
        await asyncio.sleep(5)
        try:
            token = await kiwoom_bridge.get_access_token()
            if not token:
                continue
            data = await kiwoom_bridge.get_filled_orders(token)
            matched = next(
                (o for o in data.get("cntr", []) if o.get("ord_no") == ord_no),
                None,
            )
            if not matched:
                continue  # 아직 미체결

            cntr_pric = int(matched.get("cntr_pric") or 0)
            cntr_qty  = int(matched.get("cntr_qty")  or 0)
            io_tp     = matched.get("io_tp_nm", "")
            action    = "BUY" if "매수" in io_tp else "SELL"
            cmsn      = int(matched.get("tdy_trde_cmsn") or 0)
            tax       = int(matched.get("tdy_trde_tax")  or 0)
            stk_nm    = (matched.get("stk_nm") or "").strip()

            async with AsyncSessionLocal() as db:
                res    = await db.execute(select(TradeRecord).where(TradeRecord.order_id == ord_no))
                record = res.scalar_one_or_none()
                if not record or record.status != "PENDING":
                    return  # 이미 처리됨

                record.status     = "DONE"
                record.price      = cntr_pric
                record.amount     = cntr_pric * cntr_qty
                record.commission = cmsn + tax
                if stk_nm and record.stock_name in ("", record.stock_code, "종목명"):
                    record.stock_name = stk_nm

                if action == "SELL":
                    # 이동평균법으로 현재 포지션 avg_cost 계산
                    # (현재 SELL 레코드는 아직 PENDING → DONE 전이므로 쿼리에 포함 안 됨)
                    hist_res = await db.execute(
                        select(TradeRecord).where(
                            TradeRecord.stock_code == stk_cd,
                            TradeRecord.status == "DONE",
                        ).order_by(TradeRecord.created_at, TradeRecord.action.desc())
                    )
                    hist = hist_res.scalars().all()

                    inv_qty = 0; inv_cost = 0.0; inv_cmsn = 0.0
                    for tr in hist:
                        if tr.action == "BUY":
                            total     = inv_cost * inv_qty + tr.price * tr.quantity
                            inv_qty  += tr.quantity
                            inv_cost  = total / inv_qty if inv_qty else tr.price
                            inv_cmsn += tr.commission
                        elif tr.action == "SELL":
                            r = min(tr.quantity / inv_qty, 1.0) if inv_qty > 0 else 0
                            inv_qty   = max(0, inv_qty - tr.quantity)
                            inv_cmsn  = max(0.0, inv_cmsn * (1 - r))
                            if inv_qty == 0:
                                inv_cost = 0.0; inv_cmsn = 0.0

                    avg_buy      = inv_cost if inv_qty > 0 else cntr_pric
                    ratio        = min(cntr_qty / inv_qty, 1.0) if inv_qty > 0 else 1.0
                    buy_cmsn_cut = int(inv_cmsn * ratio)
                    record.profit = int((cntr_pric - avg_buy) * cntr_qty) - cmsn - tax - buy_cmsn_cut

                await db.commit()
                print(f"✅ 자동 체결 확인: {ord_no} {action} {stk_cd} {cntr_pric:,}원 profit={record.profit:+.0f}")
            return  # 완료

        except Exception as e:
            print(f"⚠️ 자동 체결 확인 오류 ({attempt+1}/12): {e}")

    print(f"⏰ 자동 체결 확인 타임아웃: {ord_no} — 수동 동기화 필요")

class SignalResponse(BaseModel):
    accepted:  bool
    signal_id: str
    message:   str

def verify_signal_signature(body: str, signature: str) -> bool:
    expected = hmac.new(SIGNAL_SECRET.encode(), body.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected.encode('utf-8'), signature.encode('utf-8'))

def risk_check(signal: TradingSignal) -> tuple[bool, str]:
    if signal.confidence < 0.6:
        return False, f"신뢰도 부족: {signal.confidence:.0%} (최소 60% 필요)"
    if signal.action == "HOLD":
        return False, "HOLD 신호 - 주문 없음"
    if not signal.stock_code.isdigit() or len(signal.stock_code) != 6:
        return False, f"잘못된 종목코드: {signal.stock_code}"
    return True, "ok"

@router.post("/receive", response_model=SignalResponse)
async def receive_signal(
    request: Request,
    x_signal_signature: Optional[str] = Header(None),
    db: AsyncSession = Depends(get_db),
    _ip: None = Depends(require_allowed_ip),
):
    """신호 수신 - ALLOWED_IPS에 등록된 IP(Windows PC)만 가능"""
    # 요청 본문 읽기
    body = await request.body()
    body_str = body.decode('utf-8')
    
    # JSON 파싱
    import json
    signal_data = json.loads(body_str)
    
    # signal_id가 없으면 자동 생성
    if 'signal_id' not in signal_data:
        signal_data['signal_id'] = str(uuid.uuid4())
    
    # Pydantic 모델 생성
    signal = TradingSignal(**signal_data)
    
    # 서명 검증
    if x_signal_signature:
        if not verify_signal_signature(body_str, x_signal_signature):
            raise HTTPException(status_code=403, detail="서명 검증 실패")

    existing = await db.execute(
        select(SignalRecord).where(SignalRecord.signal_id == signal.signal_id)
    )
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=409, detail="중복 신호")

    ok, reason = risk_check(signal)

    record = SignalRecord(
        signal_id=signal.signal_id, stock_code=signal.stock_code,
        stock_name=signal.stock_name, action=signal.action,
        confidence=signal.confidence, reason=signal.reason,
        target_price=signal.target_price,
        executed=False, rejected=not ok,
        reject_reason=reason if not ok else None,
    )
    db.add(record)
    await db.commit()

    if not ok:
        return SignalResponse(accepted=False, signal_id=signal.signal_id, message=reason)

    # ──── 키움증권 주문 실행 ────
    order_result = await kiwoom_bridge.send_order(signal, db)
    
    if not order_result["success"]:
        record.rejected = True
        record.reject_reason = order_result["message"]
        await db.commit()
        return SignalResponse(accepted=False, signal_id=signal.signal_id, 
                            message=f"주문 실패: {order_result['message']}")

    record.executed = True
    await db.commit()

    # 체결 확인 백그라운드 태스크 (5초 간격, 최대 60초 재시도)
    ord_no = order_result.get("order_id")
    if ord_no:
        asyncio.create_task(_auto_sync_order(ord_no, signal.stock_code))

    return SignalResponse(accepted=True, signal_id=signal.signal_id,
                          message=order_result["message"])

@router.post("/callback")
async def signal_callback(
    data: dict,
    _ip: None = Depends(require_allowed_ip),
    db: AsyncSession = Depends(get_db),
):
    """키움 브릿지 체결 결과 수신 - ALLOWED_IPS만 가능"""
    result = await db.execute(
        select(SignalRecord).where(SignalRecord.signal_id == data.get("signal_id"))
    )
    record = result.scalar_one_or_none()
    if record:
        record.executed = data.get("status") == "DONE"
        await db.commit()
    return {"ok": True}

@router.get("/list")
async def list_signals(limit: int = 50, db: AsyncSession = Depends(get_db), _=Depends(require_session)):
    """신호 목록 (로그인 필요)"""
    from database import TradeRecord
    signals = (await db.execute(
        select(SignalRecord).order_by(SignalRecord.created_at.desc()).limit(limit)
    )).scalars().all()

    # signal_id → 체결가 매핑
    sids = [s.signal_id for s in signals]
    trades = (await db.execute(
        select(TradeRecord).where(TradeRecord.signal_id.in_(sids))
    )).scalars().all()
    executed_price = {t.signal_id: t.price for t in trades}

    return [
        {
            "signal_id":     r.signal_id,
            "created_at":    r.created_at.isoformat(),
            "stock_code":    r.stock_code,
            "stock_name":    r.stock_name,
            "action":        r.action,
            "confidence":    r.confidence,
            "reason":        r.reason,
            "target_price":  r.target_price,
            "executed_price": executed_price.get(r.signal_id),
            "executed":      r.executed,
            "rejected":      r.rejected,
            "reject_reason": r.reject_reason,
        }
        for r in signals
    ]
