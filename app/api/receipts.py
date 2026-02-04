from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from shutil import copyfileobj
from uuid import uuid4

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile, status
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, get_db
from app.models.receipt import Receipt
from app.models.receipt_item import ReceiptItem
from app.models.receipt_task import ReceiptTask
from app.models.user import User
from app.schemas.receipt import ReceiptOut

import base64
import mimetypes
from openai import OpenAI
from app.core.config import settings


router = APIRouter(prefix="/receipts", tags=["receipts"])

UPLOAD_DIR = Path("app/uploads")
ALLOWED_CURRENCIES = {"USD", "EUR", "CHF"}


@router.post("/upload", response_model=ReceiptOut, status_code=status.HTTP_201_CREATED)
def upload_receipt(
    file: UploadFile = File(...),
    currency: str = Form("USD"),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    # validate currency
    currency = (currency or "AUTO").upper()
    if currency not in {"AUTO", "USD", "EUR", "CHF", "RUB"}:
        raise HTTPException(status_code=400, detail="currency must be one of AUTO, USD, EUR, CHF, RUB")


    # validate file
    if not file.content_type or not file.content_type.startswith("image/"):
        raise HTTPException(status_code=400, detail="Only image uploads are supported")

    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

    # save file
    orig_suffix = Path(file.filename).suffix.lower() if file.filename else ""
    suffix = orig_suffix if orig_suffix in {".png", ".jpg", ".jpeg", ".webp"} else ".jpg"
    safe_name = f"{uuid4().hex}{suffix}"
    save_path = UPLOAD_DIR / safe_name

    try:
        with save_path.open("wb") as out:
            copyfileobj(file.file, out)
    finally:
        try:
            file.file.close()
        except Exception:
            pass

    # create receipt + enqueue task
    try:
        receipt = Receipt(
            user_id=user.id,
            status="queued",
            version=1,
            currency=currency,
            image_path=str(save_path).replace("\\", "/"),
        )
        db.add(receipt)
        db.flush()  # get receipt.id

        task = ReceiptTask(
            receipt_id=receipt.id,
            status="queued",
            receipt_version=receipt.version,
            run_after=datetime.now(timezone.utc),
        )
        db.add(task)

        db.commit()
        db.refresh(receipt)
        return receipt

    except Exception as e:
        db.rollback()
        try:
            if save_path.exists():
                save_path.unlink()
        except Exception:
            pass
        raise HTTPException(status_code=500, detail=f"Failed to enqueue receipt processing: {e}")


@router.post("/{receipt_id}/reprocess", response_model=ReceiptOut)
def reprocess_receipt(
    receipt_id: int,
    currency: str | None = Query(None),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """
    - increments receipt.version (supersedes in-flight tasks)
    - resets parsed fields + items
    - enqueues a new ReceiptTask with the new version
    Currency is NOT changed here (it was chosen at upload).
    """
    receipt = (
        db.query(Receipt)
        .filter(Receipt.id == receipt_id, Receipt.user_id == user.id)
        .first()
    )
    if not receipt:
        raise HTTPException(status_code=404, detail="Receipt not found")

    if currency is not None:
        currency = currency.upper()
        if currency not in {"AUTO", "USD", "EUR", "CHF", "RUB"}:
            raise HTTPException(status_code=400, detail="currency must be one of AUTO, USD, EUR, CHF, RUB")

    try:
        receipt.currency = currency

        receipt.version = (receipt.version or 0) + 1
        receipt.category = "OTHER"
        receipt.status = "queued"
        receipt.error = None

        # reset extracted fields
        receipt.merchant = None
        receipt.purchase_datetime = None
        receipt.total = None

        # if you added usd fields in model, reset them too
        if hasattr(receipt, "total_usd"):
            receipt.total_usd = None  # type: ignore[attr-defined]

        receipt.raw_ocr_text = None
        receipt.raw_llm_json = None

        db.add(receipt)

        # delete old items
        db.query(ReceiptItem).filter(ReceiptItem.receipt_id == receipt.id).delete()

        # enqueue new task for the new version
        task = ReceiptTask(
            receipt_id=receipt.id,
            status="queued",
            receipt_version=receipt.version,
            run_after=datetime.now(timezone.utc),
        )
        db.add(task)

        db.commit()
        db.refresh(receipt)
        return receipt

    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Failed to reprocess receipt: {e}")


@router.get("/{receipt_id}", response_model=ReceiptOut)
def get_receipt(
    receipt_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    receipt = (
        db.query(Receipt)
        .filter(Receipt.id == receipt_id, Receipt.user_id == user.id)
        .first()
    )
    if not receipt:
        raise HTTPException(status_code=404, detail="Receipt not found")
    return receipt


@router.get("", response_model=list[ReceiptOut])
def list_receipts(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    receipts = (
        db.query(Receipt)
        .filter(Receipt.user_id == user.id)
        .order_by(Receipt.uploaded_at.desc())
        .offset(offset)
        .limit(limit)
        .all()
    )
    return receipts


@router.get("/{receipt_id}/task")
def get_receipt_task(
    receipt_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    receipt = (
        db.query(Receipt)
        .filter(Receipt.id == receipt_id, Receipt.user_id == user.id)
        .first()
    )
    if not receipt:
        raise HTTPException(status_code=404, detail="Receipt not found")

    task = (
        db.query(ReceiptTask)
        .filter(ReceiptTask.receipt_id == receipt_id)
        .order_by(ReceiptTask.created_at.desc())
        .first()
    )
    if not task:
        return {"receipt_id": receipt_id, "task": None}

    return {
        "receipt_id": receipt_id,
        "task": {
            "id": task.id,
            "status": task.status,
            "attempts": task.attempts,
            "receipt_version": task.receipt_version,
            "run_after": task.run_after,
            "locked_at": task.locked_at,
            "locked_by": task.locked_by,
            "last_error": task.last_error,
            "created_at": task.created_at,
            "updated_at": task.updated_at,
        },
        "receipt_version": receipt.version,
        "receipt_status": receipt.status,
        "receipt_currency": receipt.currency,
    }

@router.post("/detect-currency")
def detect_currency(
    file: UploadFile = File(...),
    user: User = Depends(get_current_user),
):
    if not file.content_type or not file.content_type.startswith("image/"):
        raise HTTPException(status_code=400, detail="Only image uploads are supported")

    allowed = {"USD", "EUR", "CHF", "RUB"}

    # read bytes
    data = file.file.read()
    try:
        file.file.close()
    except Exception:
        pass

    mime = file.content_type or "image/jpeg"
    b64 = base64.b64encode(data).decode("utf-8")
    data_url = f"data:{mime};base64,{b64}"

    prompt = (
        "You are a classifier.\n"
        "Determine the currency used for the receipt amounts.\n"
        "Return EXACTLY one token from: USD, EUR, CHF, RUB, UNKNOWN.\n"
        "If ambiguous or not visible -> UNKNOWN.\n"
    )

    client = OpenAI(api_key=settings.OPENAI_API_KEY or None)
    resp = client.responses.create(
        model=settings.OPENAI_OCR_MODEL,  # gpt-4o-mini
        input=[{
            "role": "user",
            "content": [
                {"type": "input_text", "text": prompt},
                {"type": "input_image", "image_url": data_url},
            ],
        }],
        temperature=0,
        max_output_tokens=10,
    )

    cur = (resp.output_text or "").strip().upper()
    cur = cur.split()[0] if cur else "UNKNOWN"

    if cur not in allowed:
        cur = "USD"  # default if UNKNOWN or garbage

    return {"currency": cur}
