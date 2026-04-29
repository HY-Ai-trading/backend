from sqlalchemy import Column, Integer, String, Float, DateTime, Boolean, Text
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import DeclarativeBase, sessionmaker
from datetime import datetime

DATABASE_URL = "sqlite+aiosqlite:///./trading.db"

engine = create_async_engine(DATABASE_URL, echo=False)
AsyncSessionLocal = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

class Base(DeclarativeBase):
    pass

# 체결 / 주문 기록
class TradeRecord(Base):
    __tablename__ = "trades"
    id           = Column(Integer, primary_key=True, index=True)
    created_at   = Column(DateTime, default=datetime.now)
    stock_code   = Column(String(10))       # 종목코드 (ex: 005930)
    stock_name   = Column(String(50))       # 종목명
    action       = Column(String(4))        # BUY / SELL
    quantity     = Column(Integer)
    price        = Column(Integer)
    amount       = Column(Integer)          # 총금액
    status       = Column(String(10))       # PENDING / DONE / FAIL
    signal_id    = Column(String(36))       # 오픈클로 신호 ID
    reason       = Column(Text)             # AI 판단 이유
    profit       = Column(Float, default=0) # 실현손익 (수수료+세금 차감)
    commission   = Column(Integer, default=0) # 수수료+세금
    order_id     = Column(String(20))       # 키움 주문번호

# 오픈클로 신호 기록
class SignalRecord(Base):
    __tablename__ = "signals"
    id           = Column(Integer, primary_key=True, index=True)
    signal_id    = Column(String(36), unique=True, index=True)
    created_at   = Column(DateTime, default=datetime.now)
    stock_code   = Column(String(10))
    stock_name   = Column(String(50))
    action       = Column(String(4))        # BUY / SELL / HOLD
    confidence   = Column(Float)            # 신뢰도 0.0 ~ 1.0
    reason       = Column(Text)             # AI 분석 내용
    target_price = Column(Integer, nullable=True)   # 신호 요청 시 지정가
    executed     = Column(Boolean, default=False)
    rejected     = Column(Boolean, default=False)
    reject_reason= Column(Text)

# 일별 손익 요약
class DailySummary(Base):
    __tablename__ = "daily_summary"
    id           = Column(Integer, primary_key=True, index=True)
    date         = Column(String(10), unique=True)  # YYYY-MM-DD
    total_buy    = Column(Integer, default=0)
    total_sell   = Column(Integer, default=0)
    realized_pnl = Column(Float, default=0)
    trade_count  = Column(Integer, default=0)

async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        # 기존 DB에 order_id 컬럼이 없으면 추가
        for sql in [
            "ALTER TABLE trades ADD COLUMN order_id VARCHAR(20)",
            "ALTER TABLE trades ADD COLUMN commission INTEGER DEFAULT 0",
            "ALTER TABLE signals ADD COLUMN target_price INTEGER",
        ]:
            try:
                await conn.execute(__import__("sqlalchemy").text(sql))
            except Exception:
                pass

async def get_db():
    async with AsyncSessionLocal() as session:
        yield session
