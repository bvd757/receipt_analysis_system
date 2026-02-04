from __future__ import annotations

import base64
import mimetypes
import time
import os
from pathlib import Path

from openai import OpenAI

from app.core.config import settings


class OpenAIVisionOcrProvider:
    def __init__(self, client: OpenAI | None = None, model: str | None = None):
        api_key = settings.OPENAI_API_KEY or None
        self.client = client or OpenAI(api_key=api_key)
        self.model = model or settings.OPENAI_OCR_MODEL

    def extract_text(self, image_path: str) -> str:
        p = Path(image_path)
        if not p.exists():
            raise FileNotFoundError(f"Image not found: {p}")

        mime, _ = mimetypes.guess_type(str(p))
        mime = mime or "image/jpeg"

        b64 = base64.b64encode(p.read_bytes()).decode("utf-8")
        data_url = f"data:{mime};base64,{b64}"

        prompt = (
            "You are an OCR engine for receipts.\n"
            "Task: extract ALL visible text from the receipt image.\n"
            "Rules:\n"
            "- Output ONLY the extracted text, no commentary.\n"
            "- Preserve reading order and line breaks as much as possible.\n"
            "- If a token is unclear, keep the best guess rather than omitting.\n"
        )

        last_err: Exception | None = None
        for attempt in range(3):
            try:
                resp = self.client.responses.create(
                    model=self.model,
                    input=[{
                        "role": "user",
                        "content": [
                            {"type": "input_text", "text": prompt},
                            {"type": "input_image", "image_url": data_url},
                        ],
                    }],
                    temperature=0,
                    max_output_tokens=2000,
                )
                return (resp.output_text or "").strip()
            except Exception as e:
                last_err = e
                time.sleep(2 ** attempt)

        raise last_err
