from sqlalchemy.orm import Session

from app.models.receipt import Receipt
from app.models.receipt_item import ReceiptItem
from app.services.ocr_provider import OpenAIVisionOcrProvider
from app.services.receipt_structurer import OpenAIReceiptStructurer
from app.core.config import settings


def _fx_to_usd(curr: str) -> float:
    c = (curr or "USD").upper()
    if c == "USD":
        return 1.0
    if c == "EUR":
        return float(settings.FX_EUR_TO_USD)
    if c == "CHF":
        return float(settings.FX_CHF_TO_USD)
    if c == "RUB":
        return float(settings.FX_RUB_TO_USD)
    return 1.0


def _norm_currency(c: str | None) -> str | None:
    if not c:
        return None
    c = c.strip().upper()
    return c if c in {"USD", "EUR", "CHF", "RUB"} else None


def process_receipt(receipt_id: int, db: Session, expected_version: int) -> None:
    receipt = db.query(Receipt).filter(Receipt.id == receipt_id).first()
    if not receipt:
        return

    if receipt.version != expected_version:
        return

    receipt.status = "processing"
    receipt.error = None
    db.add(receipt)
    db.commit()

    try:
        ocr = OpenAIVisionOcrProvider()
        ocr_text = ocr.extract_text(receipt.image_path or "")
        receipt.raw_ocr_text = ocr_text
        db.add(receipt)
        db.commit()

        structurer = OpenAIReceiptStructurer()
        parsed = structurer.structure(ocr_text)

        db.refresh(receipt)
        if receipt.version != expected_version:
            return
        
        det = _norm_currency(parsed.currency)
        receipt.detected_currency = det

        if (receipt.currency or "").upper() == "AUTO":
            receipt.currency = det or "USD"

        fx = _fx_to_usd(receipt.currency)
        receipt.total_usd = (parsed.total * fx) if parsed.total is not None else None
        receipt.category = getattr(parsed, "category", "OTHER") or "OTHER"
        receipt.merchant = parsed.merchant
        receipt.purchase_datetime = parsed.purchase_datetime
        receipt.total = parsed.total
        receipt.raw_llm_json = parsed.model_dump_json(indent=2, ensure_ascii=False)

        db.query(ReceiptItem).filter(ReceiptItem.receipt_id == receipt.id).delete()
        for it in parsed.items:
            line_total = it.line_total
            if line_total is None and it.quantity is not None and it.unit_price is not None:
                line_total = it.quantity * it.unit_price

            db.add(
                ReceiptItem(
                    receipt_id=receipt.id,
                    name=it.name,
                    quantity=it.quantity,
                    unit_price=it.unit_price,
                    line_total=line_total,
                    line_total_usd=(line_total * fx) if line_total is not None else None,
                )
            )


        receipt.status = "done"
        db.add(receipt)
        db.commit()

    except Exception as e:
        db.rollback()
        receipt.status = "error"
        receipt.error = str(e)
        db.add(receipt)
        db.commit()
