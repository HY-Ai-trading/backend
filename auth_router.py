from fastapi import APIRouter, HTTPException, Header
from fastapi.security import OAuth2PasswordRequestForm
from fastapi import Depends
from pydantic import BaseModel
from auth import DASHBOARD_PASSWORD, create_session, validate_session

router = APIRouter(prefix="/auth", tags=["인증"])


class LoginRequest(BaseModel):
    password: str


@router.post("/login")
async def login(req: LoginRequest):
    if req.password != DASHBOARD_PASSWORD:
        raise HTTPException(status_code=401, detail="비밀번호 틀림")
    token = create_session()
    return {"ok": True, "token": token}


@router.post("/token", summary="Swagger 로그인 (username 무관, password만 입력)")
async def token_for_docs(form: OAuth2PasswordRequestForm = Depends()):
    """Swagger UI 우상단 Authorize 버튼용. username은 아무 값, password만 맞으면 됩니다."""
    if form.password != DASHBOARD_PASSWORD:
        raise HTTPException(status_code=401, detail="비밀번호 틀림")
    token = create_session()
    return {"access_token": token, "token_type": "bearer"}


@router.post("/logout")
async def logout():
    return {"ok": True}


@router.get("/check")
async def check(authorization: str | None = Header(default=None)):
    token = authorization.removeprefix("Bearer ") if authorization else None
    return {"authenticated": validate_session(token)}
