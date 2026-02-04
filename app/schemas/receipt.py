from datetime import datetime
from pydantic import BaseModel


class ReceiptItemOut(BaseModel):
    id: int
    name: str
    quantity: float | None = None
    unit_price: float | None = None
    line_total: float | None = None
    line_total_usd: float | None = None

    model_config = {"from_attributes": True}


class ReceiptOut(BaseModel):
    id: int
    status: str
    uploaded_at: datetime

    merchant: str | None = None
    total: float | None = None
    currency: str | None = None
    total_usd: float | None = None
    purchase_datetime: datetime | None = None

    image_path: str | None = None
    raw_ocr_text: str | None = None
    raw_llm_json: str | None = None

    detected_currency: str | None = None
    category: str | None = None

    error: str | None = None

    items: list[ReceiptItemOut] = []

    model_config = {"from_attributes": True}
