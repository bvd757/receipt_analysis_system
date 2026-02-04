from __future__ import annotations

from datetime import datetime
from typing import Optional

from openai import OpenAI
from pydantic import BaseModel, Field, conlist

from app.core.config import settings
from typing import Literal
from pydantic import field_validator


Category = Literal[
    "GROCERIES",
    "CAFE",
    "RESTAURANT",
    "TRANSPORT",
    "PHARMACY",
    "UTILITIES",
    "ENTERTAINMENT",
    "CLOTHING",
    "ELECTRONICS",
    "OTHER",
]


class ParsedItem(BaseModel):
    name: str = Field(description="Product/service name as it appears on the receipt.")
    quantity: Optional[float] = Field(default=None, description="Quantity if present.")
    unit_price: Optional[float] = Field(default=None, description="Unit price if present.")
    line_total: Optional[float] = Field(default=None, description="Total price for this line if present.")


class ParsedReceipt(BaseModel):
    merchant: Optional[str] = Field(default=None, description="Merchant/store name.")
    purchase_datetime: Optional[datetime] = Field(
        default=None,
        description="Purchase date and time if present. If only date is present, set time to 00:00:00.",
    )
    total: Optional[float] = Field(default=None, description="Grand total paid by customer.")
    currency: Optional[str] = Field(
        default=None,
        description="Currency code or symbol (prefer ISO-4217 like USD/EUR if possible).",
    )
    category: Category = Field(
        default="OTHER",
        description=(
            "Expense category. Must be exactly one of: "
            "GROCERIES, CAFE, RESTAURANT, TRANSPORT, PHARMACY, UTILITIES, "
            "ENTERTAINMENT, CLOTHING, ELECTRONICS, OTHER"
        ),
    )
    items: conlist(ParsedItem, max_length=50) = Field(
        default_factory=list,
        description="Line items purchased. If there are more than 50, include the first 50 most relevant ones.",
    )

    @field_validator("category", mode="before")
    @classmethod
    def _norm_category(cls, v):
        if not v:
            return "OTHER"
        s = str(v).strip().upper()

        # немного синонимов (достаточно)
        if s in {"CAFE", "CAFÉ", "COFFEE", "COFFEESHOP", "BAR"} or "КАФ" in s or "КОФ" in s:
            return "CAFE"
        if s in {"RESTAURANT", "DINER"} or "РЕСТ" in s:
            return "RESTAURANT"
        if s in {"GROCERY", "GROCERIES", "SUPERMARKET"} or "СУПЕР" in s or "МАГАЗ" in s:
            return "GROCERIES"
        if s in {"TRANSPORT", "TAXI", "UBER", "BUS", "METRO"} or "ТАКС" in s or "МЕТРО" in s:
            return "TRANSPORT"
        if s in {"PHARMACY", "DRUGSTORE"} or "АПТ" in s:
            return "PHARMACY"
        if s in {"UTILITIES", "BILLS"} or "КОММУН" in s:
            return "UTILITIES"
        if s in {"ENTERTAINMENT", "CINEMA", "MOVIE"} or "КИНО" in s:
            return "ENTERTAINMENT"
        if s in {"CLOTHING", "APPAREL"} or "ОДЕЖ" in s:
            return "CLOTHING"
        if s in {"ELECTRONICS"} or "ЭЛЕКТР" in s:
            return "ELECTRONICS"

        allowed = {
            "GROCERIES","CAFE","RESTAURANT","TRANSPORT","PHARMACY",
            "UTILITIES","ENTERTAINMENT","CLOTHING","ELECTRONICS","OTHER"
        }
        return s if s in allowed else "OTHER"



class OpenAIReceiptStructurer:
    def __init__(self, client: OpenAI | None = None, model: str | None = None):
        api_key = settings.OPENAI_API_KEY or None
        self.client = client or OpenAI(api_key=api_key)
        self.model = model or getattr(settings, "OPENAI_STRUCT_MODEL", settings.OPENAI_OCR_MODEL)

    def structure(self, ocr_text: str) -> ParsedReceipt:
        system = (
            "You are a receipt information extraction engine.\n"
            "Extract structured fields from OCR text of a receipt.\n"
            "Rules:\n"
            "- If a value is not present, return null.\n"
            "- Do NOT invent values.\n"
            "- Items: include purchased line items; exclude headers and footers.\n"
            "- If only a date is present without time, set time to 00:00:00.\n"
            "- Prefer currency as ISO-4217 code if obvious, otherwise keep symbol.\n"
            "- If there are many items, include at most 50 items.\n"
            "- Determine category from the receipt and return category as EXACTLY one of: "
            "GROCERIES, CAFE, RESTAURANT, TRANSPORT, PHARMACY, UTILITIES, ENTERTAINMENT, CLOTHING, ELECTRONICS, OTHER.\n"
            "- If uncertain, use OTHER.\n"

        )

        # Attempt 1: full extraction with larger output budget
        # Attempt 2 (fallback): header-only fields, items=[]
        last_err: Exception | None = None
        for attempt in range(2):
            try:
                if attempt == 0:
                    user = f"OCR TEXT:\n{ocr_text}\n\nIMPORTANT: include at most 50 items."
                    max_out = 2500
                else:
                    user = (
                        f"OCR TEXT:\n{ocr_text}\n\n"
                        "FALLBACK MODE: Return only merchant, purchase_datetime, total, currency. "
                        "Set items to an empty list []."
                    )
                    max_out = 1200

                resp = self.client.responses.parse(
                    model=self.model,
                    input=[
                        {"role": "system", "content": system},
                        {"role": "user", "content": user},
                    ],
                    text_format=ParsedReceipt,
                    temperature=0,
                    max_output_tokens=max_out,
                )
                return resp.output_parsed

            except Exception as e:
                last_err = e

        raise last_err  # type: ignore
