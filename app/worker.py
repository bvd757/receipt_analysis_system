import os
import time
from datetime import datetime, timedelta, timezone

from sqlalchemy import text

from app.core.db import SessionLocal
from app.services.receipt_processor import process_receipt

POLL_SECONDS = int(os.getenv("WORKER_POLL_SECONDS", "2"))
WORKER_ID = os.getenv("WORKER_ID", "worker-1")
MAX_ATTEMPTS = int(os.getenv("TASK_MAX_ATTEMPTS", "3"))

# через сколько секунд считаем "processing" задачу зависшей
LOCK_TIMEOUT_SECONDS = int(os.getenv("TASK_LOCK_TIMEOUT_SECONDS", "180"))


REQUEUE_STALE_SQL = text("""
UPDATE receipt_tasks
SET
  status='queued',
  locked_at=NULL,
  locked_by=NULL,
  run_after=:now,
  updated_at=:now
WHERE status='processing'
  AND locked_at IS NOT NULL
  AND locked_at <= :stale_before;
""")


CLAIM_SQL = text("""
UPDATE receipt_tasks
SET
  status='processing',
  locked_at=:now,
  locked_by=:worker,
  attempts=attempts+1,
  updated_at=:now
WHERE id = (
  SELECT id
  FROM receipt_tasks
  WHERE status='queued' AND run_after <= :now
  ORDER BY created_at
  LIMIT 1
)
RETURNING id, receipt_id, receipt_version, attempts;
""")


def mark_done(db, task_id: int):
    now = datetime.now(timezone.utc)
    db.execute(
        text("""
        UPDATE receipt_tasks
        SET status='done', locked_at=NULL, locked_by=NULL, updated_at=:now
        WHERE id=:id;
        """),
        {"now": now, "id": task_id},
    )
    db.commit()


def mark_failed(db, task_id: int, attempts: int, err: str):
    now = datetime.now(timezone.utc)

    if attempts >= MAX_ATTEMPTS:
        db.execute(
            text("""
            UPDATE receipt_tasks
            SET status='error', last_error=:err, locked_at=NULL, locked_by=NULL, updated_at=:now
            WHERE id=:id;
            """),
            {"err": err, "now": now, "id": task_id},
        )
        db.commit()
        return

    delay_minutes = 2 ** (attempts - 1)  # 1,2,4...
    run_after = now + timedelta(minutes=delay_minutes)

    db.execute(
        text("""
        UPDATE receipt_tasks
        SET status='queued', run_after=:run_after, last_error=:err,
            locked_at=NULL, locked_by=NULL, updated_at=:now
        WHERE id=:id;
        """),
        {"run_after": run_after, "err": err, "now": now, "id": task_id},
    )
    db.commit()


def set_receipt_processing(db, receipt_id: int):
    now = datetime.now(timezone.utc)
    db.execute(
        text("""
        UPDATE receipts
        SET status='processing'
        WHERE id=:rid;
        """),
        {"rid": receipt_id},
    )
    db.commit()


def main():
    print(f"[worker] started worker_id={WORKER_ID} poll={POLL_SECONDS}s "
          f"max_attempts={MAX_ATTEMPTS} lock_timeout={LOCK_TIMEOUT_SECONDS}s")

    while True:
        db = SessionLocal()
        try:
            now = datetime.now(timezone.utc)

            # 0) requeue stale "processing" tasks
            stale_before = now - timedelta(seconds=LOCK_TIMEOUT_SECONDS)
            db.execute(REQUEUE_STALE_SQL, {"now": now, "stale_before": stale_before})
            db.commit()

            # 1) claim one task
            result = db.execute(CLAIM_SQL, {"now": now, "worker": WORKER_ID})
            row = result.mappings().first()
            result.close()  # <-- важно для sqlite + RETURNING
            db.commit()

            if not row:
                time.sleep(POLL_SECONDS)
                continue

            # Всё после claim — под защитой, чтобы не оставлять processing навсегда
            try:
                task_id = int(row["id"])
                receipt_id = int(row["receipt_id"])
                receipt_version = int(row["receipt_version"])
                attempts = int(row["attempts"])
            except Exception as e:
                # если даже распарсить row не смогли — переводим задачу в error
                # (task_id может быть неизвестен, но обычно есть)
                try:
                    task_id = int(row.get("id", 0)) if row else 0
                except Exception:
                    task_id = 0
                if task_id:
                    mark_failed(db, task_id, MAX_ATTEMPTS, f"worker parse error: {e}")
                continue

            try:
                # 2) обновим receipt.status сразу, чтобы UI не висел на queued
                set_receipt_processing(db, receipt_id)

                # 3) обработка (OCR + structuring)
                process_receipt(receipt_id, db, expected_version=receipt_version)

                # 4) done
                mark_done(db, task_id)
                print(f"[worker] task={task_id} receipt={receipt_id} done")

            except Exception as e:
                db.rollback()
                mark_failed(db, task_id, attempts, str(e))
                print(f"[worker] task={task_id} receipt={receipt_id} failed: {e}")

        finally:
            db.close()


if __name__ == "__main__":
    main()
