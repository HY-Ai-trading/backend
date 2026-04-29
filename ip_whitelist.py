"""
ip_whitelist.py
허용된 IP에서만 매수/매도 신호를 받을 수 있도록 제한
대시보드 조회는 모든 IP 허용 (로컬에서 브라우저로 볼 수 있게)
"""

from fastapi import Request, HTTPException
import os
from dotenv import load_dotenv

load_dotenv()

# .env의 ALLOWED_IPS=192.168.1.10,192.168.1.11 형식으로 입력
_raw = os.getenv("ALLOWED_IPS", "")
ALLOWED_IPS: set[str] = set(ip.strip() for ip in _raw.split(",") if ip.strip())

def get_client_ip(request: Request) -> str:
    """실제 클라이언트 IP 추출 (프록시 뒤에 있을 경우도 처리)"""
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host

def require_allowed_ip(request: Request):
    """
    매수/매도 관련 엔드포인트에 Depends()로 사용
    허용 IP 목록이 비어있으면 전체 차단 (설정 안 하면 아무도 못 씀)
    """
    if not ALLOWED_IPS:
        raise HTTPException(
            status_code=503,
            detail="ALLOWED_IPS가 설정되지 않았습니다. .env를 확인하세요."
        )

    client_ip = get_client_ip(request)

    if client_ip not in ALLOWED_IPS:
        print(f"🚫 차단된 IP 접근 시도: {client_ip}")
        raise HTTPException(
            status_code=403,
            detail=f"허용되지 않은 IP입니다: {client_ip}"
        )

    print(f"✅ 허용된 IP 접근: {client_ip}")
