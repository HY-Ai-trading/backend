import os, secrets
from datetime import datetime, timedelta, timezone
from fastapi import Header, HTTPException, Request, Security
from fastapi.security import OAuth2PasswordBearer
from dotenv import load_dotenv
import jwt

load_dotenv()

DASHBOARD_PASSWORD = os.getenv("DASHBOARD_PASSWORD", "changeme")
API_KEY            = os.getenv("SIGNAL_SECRET_KEY", "")
SESSION_SECRET     = os.getenv("SESSION_SECRET", secrets.token_hex(32))
ALLOWED_IPS        = {ip.strip() for ip in os.getenv("ALLOWED_IPS", "127.0.0.1").split(",") if ip.strip()}
SESSION_TTL_DAYS   = 7
ALGORITHM          = "HS256"


def create_session() -> str:
    payload = {
        "exp": datetime.now(timezone.utc) + timedelta(days=SESSION_TTL_DAYS),
        "iat": datetime.now(timezone.utc),
    }
    return jwt.encode(payload, SESSION_SECRET, algorithm=ALGORITHM)


def validate_session(token: str | None) -> bool:
    if not token:
        return False
    try:
        jwt.decode(token, SESSION_SECRET, algorithms=[ALGORITHM])
        return True
    except jwt.PyJWTError:
        return False


_oauth2 = OAuth2PasswordBearer(tokenUrl="/auth/token", auto_error=False)


def require_session(
    request: Request,
    oauth_token: str | None = Security(_oauth2),
    x_api_key: str | None = Header(default=None),
):
    """Bearer 토큰 (Swagger Authorize 또는 Authorization 헤더) OR X-Api-Key"""
    # 1) JWT Bearer 토큰
    if validate_session(oauth_token):
        return

    # 2) API 키 헤더
    if x_api_key and API_KEY and secrets.compare_digest(x_api_key, API_KEY):
        return

    # 3) ALLOWED_IPS 허용 (127.0.0.1 + .env에 등록된 Windows PC 등)
    client_ip = request.client.host if request.client else ""
    if client_ip in ALLOWED_IPS:
        return

    raise HTTPException(status_code=401, detail="로그인 필요")
