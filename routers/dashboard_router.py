from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from database import get_db, TradeRecord, DailySummary, SignalRecord
from datetime import datetime, date, timedelta
from auth import require_session
import kiwoom_bridge

router = APIRouter(prefix="/dashboard", tags=["대시보드"])

@router.get("/summary")
async def get_summary(db: AsyncSession = Depends(get_db), _=Depends(require_session)):
    today = date.today().isoformat()
    week_ago = datetime.now() - timedelta(days=7)

    today_row = await db.execute(
        select(
            func.sum(TradeRecord.profit).label("pnl"),
            func.count(TradeRecord.id).label("cnt"),
        ).where(
            TradeRecord.status == "DONE",
            TradeRecord.action == "SELL",
            func.date(TradeRecord.created_at) == today,
        )
    )
    today_data = today_row.one()

    total_pnl = await db.execute(
        select(func.sum(TradeRecord.profit)).where(
            TradeRecord.status == "DONE",
            TradeRecord.action == "SELL",
        )
    )
    signal_count = await db.execute(
        select(func.count(SignalRecord.id)).where(SignalRecord.created_at >= week_ago)
    )
    return {
        "today":          today,
        "today_pnl":      today_data.pnl or 0,
        "today_trades":   today_data.cnt or 0,
        "total_pnl":      total_pnl.scalar() or 0,
        "weekly_signals": signal_count.scalar() or 0,
    }

@router.get("/trades")
async def get_trades(limit: int = 100, db: AsyncSession = Depends(get_db), _=Depends(require_session)):
    result = await db.execute(
        select(TradeRecord)
        .where(TradeRecord.order_id.isnot(None))
        .order_by(TradeRecord.created_at.desc())
        .limit(limit)
    )
    return [
        {
            "id": r.id, "created_at": r.created_at.isoformat(),
            "stock_code": r.stock_code, "stock_name": r.stock_name,
            "action": r.action, "quantity": r.quantity,
            "price": r.price, "amount": r.amount,
            "status": r.status, "profit": r.profit,
            "order_id": r.order_id, "reason": r.reason,
        }
        for r in result.scalars().all()
    ]

@router.get("/pnl-chart")
async def get_pnl_chart(days: int = 30, db: AsyncSession = Depends(get_db), _=Depends(require_session)):
    start = (date.today() - timedelta(days=days)).isoformat()
    result = await db.execute(
        select(
            func.date(TradeRecord.created_at).label("date"),
            func.sum(TradeRecord.profit).label("daily_pnl"),
            func.count(TradeRecord.id).label("trade_count"),
        )
        .where(
            TradeRecord.status == "DONE",
            TradeRecord.action == "SELL",
            func.date(TradeRecord.created_at) >= start,
        )
        .group_by(func.date(TradeRecord.created_at))
        .order_by(func.date(TradeRecord.created_at))
    )
    cumulative = 0
    data = []
    for row in result.all():
        daily = row.daily_pnl or 0
        cumulative += daily
        data.append({
            "date": str(row.date),
            "daily_pnl": daily,
            "cumulative": cumulative,
            "trade_count": row.trade_count,
        })
    return data

@router.get("/account")
async def get_account_status(db: AsyncSession = Depends(get_db), _=Depends(require_session)):
    """계좌 요약 (DB 기반 포지션 + 키움 계좌번호)"""
    result = await db.execute(
        select(TradeRecord).where(TradeRecord.order_id.isnot(None))
    )
    trades = result.scalars().all()

    positions: dict = {}
    for t in trades:
        code = t.stock_code
        if code not in positions:
            positions[code] = {"stock_code": code, "stock_name": t.stock_name, "quantity": 0, "total_cost": 0}
        if t.action == "BUY":
            positions[code]["quantity"] += t.quantity
            positions[code]["total_cost"] += t.amount
        elif t.action == "SELL":
            positions[code]["quantity"] -= t.quantity
            positions[code]["total_cost"] -= t.amount

    holdings = []
    total_cost = 0
    for p in positions.values():
        if p["quantity"] > 0:
            avg_price = int(p["total_cost"] / p["quantity"]) if p["quantity"] else 0
            holdings.append({
                "stock_code": p["stock_code"],
                "stock_name": p["stock_name"],
                "quantity": p["quantity"],
                "avg_price": avg_price,
                "total_cost": p["total_cost"],
            })
            total_cost += p["total_cost"]

    total_pnl = await db.execute(select(func.sum(TradeRecord.profit)))
    return {
        "holdings": holdings,
        "total_cost": total_cost,
        "total_realized_pnl": total_pnl.scalar() or 0,
        "position_count": len(holdings),
    }


@router.get("/positions")
async def get_positions(db: AsyncSession = Depends(get_db), _=Depends(require_session)):
    """보유 포지션 (DB 기반 순매수 계산)"""
    result = await db.execute(
        select(TradeRecord).where(TradeRecord.order_id.isnot(None))
    )
    trades = result.scalars().all()

    positions: dict = {}
    for t in trades:
        code = t.stock_code
        if code not in positions:
            positions[code] = {"stock_code": code, "stock_name": t.stock_name, "quantity": 0, "total_cost": 0}
        if t.action == "BUY":
            positions[code]["quantity"] += t.quantity
            positions[code]["total_cost"] += t.amount
        elif t.action == "SELL":
            positions[code]["quantity"] -= t.quantity
            positions[code]["total_cost"] -= t.amount

    return [
        {
            "stock_code": p["stock_code"],
            "stock_name": p["stock_name"],
            "quantity": p["quantity"],
            "avg_price": int(p["total_cost"] / p["quantity"]) if p["quantity"] else 0,
            "total_cost": p["total_cost"],
        }
        for p in positions.values()
        if p["quantity"] > 0
    ]
