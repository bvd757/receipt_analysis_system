import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path


def run_curl(args_list: list[str]) -> str:
    curl_bin = "curl.exe" if os.name == "nt" else "curl"

    cmd = [curl_bin, "-sS"] + args_list
    p = subprocess.run(cmd, capture_output=True, text=True)

    if p.returncode != 0:
        print("\n[curl error] command:", " ".join(cmd))
        print("[stdout]:", p.stdout)
        print("[stderr]:", p.stderr)
        raise SystemExit(p.returncode)

    return p.stdout.strip()


def try_parse_json(s: str):
    try:
        return json.loads(s)
    except Exception:
        return None


def register(base_url: str, username: str, password: str) -> None:
    url = f"{base_url}/auth/register"
    payload = {"username": username, "password": password}

    out = run_curl([
        "-X", "POST", url,
        "-H", "Content-Type: application/json",
        "-d", json.dumps(payload),
    ])
    js = try_parse_json(out)

    if js and isinstance(js, dict) and js.get("detail") == "Username already exists":
        print("[register] user already exists -> ok")
        return

    if js is None:
        print("[register] non-json response:", out)
        return

    if "id" in js:
        print(f"[register] ok id={js['id']} username={js.get('username')}")
    else:
        print("[register] response:", js)


def login(base_url: str, username: str, password: str) -> str:
    url = f"{base_url}/auth/login"

    out = run_curl([
        "-X", "POST", url,
        "-H", "Content-Type: application/x-www-form-urlencoded",
        "--data-urlencode", f"username={username}",
        "--data-urlencode", f"password={password}",
    ])
    js = try_parse_json(out)
    if not js or "access_token" not in js:
        print("[login] unexpected response:", out)
        raise SystemExit(1)

    token = js["access_token"]
    print("[login] ok token_len=", len(token))
    return token


def upload_receipt(base_url: str, token: str, image_path: str) -> dict:
    url = f"{base_url}/receipts/upload"

    img = Path(image_path).expanduser().resolve()
    if not img.exists():
        raise SystemExit(f"Image not found: {img}")

    out = run_curl([
        "-X", "POST", url,
        "-H", f"Authorization: Bearer {token}",
        "-F", f"file=@{str(img)}",
    ])
    js = try_parse_json(out)
    if not js or "id" not in js:
        print("[upload] unexpected response:", out)
        raise SystemExit(1)

    print(f"[upload] ok receipt_id={js['id']} status={js.get('status')}")
    return js


def reprocess_receipt(base_url: str, token: str, receipt_id: int) -> dict:
    url = f"{base_url}/receipts/{receipt_id}/reprocess"
    out = run_curl([
        "-X", "POST", url,
        "-H", f"Authorization: Bearer {token}",
    ])
    js = try_parse_json(out)
    if not js or "id" not in js:
        print("[reprocess] unexpected response:", out)
        raise SystemExit(1)

    print(f"[reprocess] ok receipt_id={js['id']} status={js.get('status')}")
    return js


def get_receipt(base_url: str, token: str, receipt_id: int) -> dict:
    url = f"{base_url}/receipts/{receipt_id}"
    out = run_curl([
        "-X", "GET", url,
        "-H", f"Authorization: Bearer {token}",
    ])
    js = try_parse_json(out)
    if js is None:
        print("[get_receipt] non-json response:", out)
        raise SystemExit(1)
    return js


def get_task(base_url: str, token: str, receipt_id: int) -> dict:
    url = f"{base_url}/receipts/{receipt_id}/task"
    out = run_curl([
        "-X", "GET", url,
        "-H", f"Authorization: Bearer {token}",
    ])
    js = try_parse_json(out)
    return js if js is not None else {"raw": out}


def poll_until_finished(
    base_url: str,
    token: str,
    receipt_id: int,
    timeout_s: int,
    interval_s: float,
    show_task: bool,
) -> dict:
    start = time.time()
    last = None

    while True:
        r = get_receipt(base_url, token, receipt_id)
        last = r
        status = r.get("status")

        merchant = r.get("merchant")
        total = r.get("total")
        currency = r.get("currency")

        extra = []
        if merchant:
            extra.append(f"merchant={merchant}")
        if total is not None:
            extra.append(f"total={total}{currency or ''}")

        extra_str = (" | " + ", ".join(extra)) if extra else ""
        print(f"[poll] receipt_id={receipt_id} status={status}{extra_str}")

        if show_task:
            t = get_task(base_url, token, receipt_id)
            task = (t.get("task") or {}) if isinstance(t, dict) else {}
            if task:
                print(
                    "       "
                    f"task_status={task.get('status')} "
                    f"attempts={task.get('attempts')} "
                    f"receipt_version={task.get('receipt_version')} "
                    f"locked_by={task.get('locked_by')}"
                )
            else:
                rv = t.get("receipt_version") if isinstance(t, dict) else None
                print(f"       task=None receipt_version={rv}")

        if status in ("done", "error"):
            print("[poll] finished.")
            return r

        if time.time() - start > timeout_s:
            print("[poll] timeout reached.")
            print(json.dumps(last, ensure_ascii=False, indent=2))
            return last

        time.sleep(interval_s)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", default="http://127.0.0.1:8000", help="Base URL for API")
    ap.add_argument("--username", default="boris", help="Username")
    ap.add_argument("--password", default="secret123", help="Password")
    ap.add_argument("--image", required=True, help="Path to receipt image, e.g. C:\\path\\to\\receipt.jpg")
    ap.add_argument("--timeout", type=int, default=120, help="Polling timeout seconds")
    ap.add_argument("--interval", type=float, default=2.0, help="Polling interval seconds")
    ap.add_argument("--show-task", action="store_true", help="Also call /receipts/{id}/task during polling")

    ap.add_argument("--reprocess-times", type=int, default=1, help="How many times to call /reprocess after first done (0 disables)")

    args = ap.parse_args()

    print("== Receipt API smoke test via curl ==")
    print("NOTE: worker должен быть запущен: python -m app.worker\n")

    register(args.base_url, args.username, args.password)
    token = login(args.base_url, args.username, args.password)

    receipt = upload_receipt(args.base_url, token, args.image)
    receipt_id = int(receipt["id"])

    r1 = poll_until_finished(args.base_url, token, receipt_id, args.timeout, args.interval, args.show_task)

    for i in range(max(0, args.reprocess_times)):
        print(f"\n== REPROCESS #{i+1}/{args.reprocess_times} ==")
        reprocess_receipt(args.base_url, token, receipt_id)
        rN = poll_until_finished(args.base_url, token, receipt_id, args.timeout, args.interval, args.show_task)

        if rN.get("status") == "done":
            print("[reprocess] done. extracted fields present:",
                  "merchant" if rN.get("merchant") else "merchant=None", "|",
                  "total" if rN.get("total") is not None else "total=None")
        else:
            print("[reprocess] finished with status:", rN.get("status"))

    print("\n== TEST COMPLETED ==")


if __name__ == "__main__":
    main()
