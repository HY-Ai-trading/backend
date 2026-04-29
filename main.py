import os
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import OAuth2PasswordBearer
from contextlib import asynccontextmanager
from dotenv import load_dotenv
from database import init_db
from routers import signal_router, dashboard_router, kiwoom_router
from auth_router import router as auth_router

load_dotenv()

@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    print("✅ DB 초기화 완료")
    yield

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
