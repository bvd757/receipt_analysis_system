import argparse
import os
import time
from pathlib import Path

import requests
import mimetypes


IMG_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff"}


def register(base_url: str, username: str, password: str) -> None:
    url = f"{base_url}/auth/register"
    r = requests.post(url, json={"username": username, "password": password}, timeout=20)
    if r.status_code in (200, 201):
        return
    if r.status_code == 400 and "already exists" in r.text.lower():
        return
    raise RuntimeError(f"register failed: {r.status_code} {r.text}")


def login(base_url: str, username: str, password: str) -> str:
    url = f"{base_url}/auth/login"
    r = requests.post(
        url,
        data={"username": username, "password": password},
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=20,
    )
    if r.status_code != 200:
        raise RuntimeError(f"login failed: {r.status_code} {r.text}")
    return r.json()["access_token"]


def upload_receipt(base_url: str, token: str, file_path: Path, currency: str = "AUTO") -> dict:
    url = f"{base_url}/receipts/upload"
    headers = {"Authorization": f"Bearer {token}"}

    mime, _ = mimetypes.guess_type(str(file_path))
    mime = (mime or "image/jpeg").lower()

    ext = file_path.suffix.lower()
    if ext in (".jpg", ".jpeg"):
        mime = "image/jpeg"
    elif ext == ".png":
        mime = "image/png"
    elif ext == ".webp":
        mime = "image/webp"

    with file_path.open("rb") as f:
        files = {"file": (file_path.name, f, mime)}
        data = {"currency": currency}
        r = requests.post(url, headers=headers, files=files, data=data, timeout=60)

    if r.status_code not in (200, 201):
        raise RuntimeError(f"upload failed: {r.status_code} {r.text}")
    return r.json()



def get_receipt(base_url: str, token: str, receipt_id: int) -> dict:
    url = f"{base_url}/receipts/{receipt_id}"
    headers = {"Authorization": f"Bearer {token}"}
    r = requests.get(url, headers=headers, timeout=20)
    if r.status_code != 200:
        raise RuntimeError(f"get_receipt failed: {r.status_code} {r.text}")
    return r.json()


def wait_receipt_done(base_url: str, token: str, receipt_id: int, timeout_s: int = 300, poll_s: float = 2.0) -> dict:
    t0 = time.time()
    last = None
    while True:
        last = get_receipt(base_url, token, receipt_id)
        st = last.get("status")
        if st in ("done", "error"):
            return last
        if time.time() - t0 > timeout_s:
            return last
        time.sleep(poll_s)


def iter_images(folder: Path):
    for p in sorted(folder.rglob("*")):
        if p.is_file() and p.suffix.lower() in IMG_EXTS:
            yield p


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", default="http://127.0.0.1:8000")
    ap.add_argument("--folder", required=True, help="Folder with receipt images")
    ap.add_argument("--username", default="bulk_user")
    ap.add_argument("--password", default="secret123")
    ap.add_argument("--currency", default="AUTO", help="AUTO recommended. Or USD/EUR/CHF/RUB.")
    ap.add_argument("--limit", type=int, default=0, help="0 = no limit")
    ap.add_argument("--sleep", type=float, default=0.0, help="Sleep between uploads (seconds)")
    ap.add_argument("--wait", action="store_true", help="Wait for processing (poll /receipts/{id})")
    ap.add_argument("--timeout", type=int, default=300, help="Wait timeout per receipt (seconds)")
    args = ap.parse_args()

    folder = Path(args.folder).expanduser().resolve()
    if not folder.exists():
        raise SystemExit(f"Folder not found: {folder}")

    # Setup session
    sess = requests.Session()

    # Register/login
    register(args.base_url, args.username, args.password)
    token = login(args.base_url, args.username, args.password)
    print(f"[auth] logged in as {args.username}")

    uploaded = []
    failures = 0

    for i, img in enumerate(iter_images(folder), start=1):
        if args.limit and i > args.limit:
            break

        try:
            resp = upload_receipt(args.base_url, token, img, currency=args.currency)
            rid = int(resp["id"])
            print(f"[upload] {i:04d} id={rid} file={img.name} status={resp.get('status')} currency_sent={args.currency}")
            uploaded.append((rid, img))

            if args.wait:
                done = wait_receipt_done(args.base_url, token, rid, timeout_s=args.timeout, poll_s=2.0)
                st = done.get("status")
                cur = done.get("currency")
                det = done.get("detected_currency")
                tot_usd = done.get("total_usd")
                err = done.get("error")
                if st == "done":
                    print(f"        -> done currency={cur} detected={det} total_usd={tot_usd}")
                else:
                    print(f"        -> {st} error={err}")

        except Exception as e:
            failures += 1
            print(f"[ERROR] file={img} err={e}")

        if args.sleep > 0:
            time.sleep(args.sleep)

    print("\n=== SUMMARY ===")
    print(f"uploaded: {len(uploaded)}")
    print(f"failures: {failures}")
    if not args.wait and uploaded:
        print("first 10 receipt_ids:", [rid for rid, _ in uploaded[:10]])


if __name__ == "__main__":
    main()
