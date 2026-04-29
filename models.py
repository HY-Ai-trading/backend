import uuid
from pydantic import BaseModel, Field
from typing import Optional

class TradingSignal(BaseModel):
    signal_id:    str = Field(default_factory=lambda: str(uuid.uuid4()))
    stock_code:   str
    stock_name:   str
    action:       str
    confidence:   float
    reason:       str
    quantity:     Optional[int] = None
    target_price: Optional[int] = None
    order_type:   Optional[str] = "LIMIT"  # LIMIT=지정가, MARKET=시장가(현재가)