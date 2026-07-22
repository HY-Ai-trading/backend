import os, asyncio
from datetime import datetime
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import OAuth2PasswordBearer
from contextlib import asynccontextmanager
from dotenv import load_dotenv
from database import init_db, AsyncSessionLocal, MonthlyAsset
from routers import signal_router, dashboard_router, kiwoom_router
from auth_router import router as auth_router
import kiwoom_bridge

load_dotenv()

async def _buy_order_price_track_loop():
    """미체결 매수 지정가 주문 가격 추적: 현재가가 내려가면 취소 후 재주문 (60초마다)"""
    await asyncio.sleep(15)  # 서버 시작 후 잠시 대기
    while True:
        try:
            async with AsyncSessionLocal() as db:
                adjustments = await kiwoom_bridge.auto_adjust_pending_buy_orders(db)
                if adjustments:
                    for a in adjustments:
                        if a["action"] == "adjusted":
                            print(f"🔄 가격조정: {a['stk_nm']} {a['old_price']:,}→{a['new_price']:,}원")
        except Exception as e:
            print(f"⚠️  매수 주문 추적 루프 오류: {e}")
        await asyncio.sleep(60)


async def _monthly_asset_snapshot_loop():
    """매월 1일, 그날의 총자산을 스냅샷으로 저장 (월별 수익률 계산 기준)"""
    while True:
        now = datetime.now()
        if now.day == 1:
            month = now.strftime("%Y-%m")
            async with AsyncSessionLocal() as db:
                if not await db.get(MonthlyAsset, month):
                    total = await kiwoom_bridge.get_total_asset()
                    if total:
                        db.add(MonthlyAsset(month=month, total_asset=total))
                        await db.commit()
                        print(f"📸 월별 총자산 스냅샷 저장: {month} = {total:,}원")
        await asyncio.sleep(3600)

@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    print("✅ DB 초기화 완료")
    task1 = asyncio.create_task(_monthly_asset_snapshot_loop())
    task2 = asyncio.create_task(_buy_order_price_track_loop())
    yield
    task1.cancel()
    task2.cancel()

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/token", auto_error=False)

app = FastAPI(
    title="키움 AI 자동매매 서버",
    version="1.0.0",
    lifespan=lifespan,
)

# Cloudflare에서 오는 실제 도메인만 허용
FRONTEND_ORIGIN = os.getenv("FRONTEND_ORIGIN", "http://localhost:3000")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[FRONTEND_ORIGIN],
    allow_credentials=True,   # 쿠키 전달 필수
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth_router)
app.include_router(signal_router.router)
app.include_router(dashboard_router.router)
app.include_router(kiwoom_router.router)

@app.get("/")
async def root():
    return {"status": "running"}

@app.get("/health")
async def health():
    return {"status": "ok"}

if __name__ == "__main__":
    import uvicorn, os
    from dotenv import load_dotenv
    load_dotenv()
    uvicorn.run("main:app", host="0.0.0.0", port=int(os.getenv("SERVER_PORT", "8000")), reload=False)
