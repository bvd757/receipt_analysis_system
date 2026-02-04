from fastapi import FastAPI
from fastapi.responses import FileResponse
from starlette.staticfiles import StaticFiles

from app.api.auth import router as auth_router
from app.api.receipts import router as receipts_router
from app.api.chat import router as chat_router

app = FastAPI(title="Receipt Analysis System")

app.include_router(auth_router)
app.include_router(receipts_router)
app.include_router(chat_router)

app.mount("/static", StaticFiles(directory="app/static"), name="static")

@app.get("/", include_in_schema=False)
def ui():
    return FileResponse("app/static/index.html")

@app.get("/health")
def health():
    return {"status": "ok"}
