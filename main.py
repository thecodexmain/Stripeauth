# main.py
# FastAPI wrapper around mentos_flow.check_card.
# Endpoints: POST /check, POST /check/bulk, GET /health, GET / (info).

import os
import time
import asyncio
from concurrent.futures import ThreadPoolExecutor
from typing import List, Optional
from fastapi import FastAPI, HTTPException, Header, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator
import re

from mentos_flow import check_card, BASE_URL

API_KEY = os.getenv("MENTOS_API_KEY", "").strip()
MAX_BULK = int(os.getenv("MENTOS_MAX_BULK", "50"))
WORKERS = int(os.getenv("MENTOS_WORKERS", "4"))

app = FastAPI(
    title="Mentos AutoStripe API",
    version="1.0.0",
    description="Four-step WooCommerce + Stripe setup-intent card validation flow.",
)
_executor = ThreadPoolExecutor(max_workers=WORKERS)


# ── Auth ──────────────────────────────────────────────────────────
def _auth(authorization: Optional[str] = Header(None)):
    if not API_KEY:
        return  # auth disabled
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="missing bearer token")
    token = authorization.removeprefix("Bearer ").strip()
    if token != API_KEY:
        raise HTTPException(status_code=403, detail="invalid token")


# ── Models ────────────────────────────────────────────────────────
class CheckRequest(BaseModel):
    card: str = Field(..., description="Card number, 12-19 digits")
    month: str = Field(..., description="Expiry month, 1-12 (zero-padded ok)")
    year: str = Field(..., description="Expiry year, 2 or 4 digits")
    cvv: str = Field(..., description="3 or 4 digit CVC")

    @field_validator("card")
    @classmethod
    def _card_ok(cls, v: str) -> str:
        v = re.sub(r"\D", "", v)
        if not (12 <= len(v) <= 19):
            raise ValueError("card must be 12-19 digits")
        return v

    @field_validator("cvv")
    @classmethod
    def _cvv_ok(cls, v: str) -> str:
        v = re.sub(r"\D", "", v)
        if not (3 <= len(v) <= 4):
            raise ValueError("cvv must be 3-4 digits")
        return v

    @field_validator("month")
    @classmethod
    def _month_ok(cls, v: str) -> str:
        v = re.sub(r"\D", "", v).zfill(2)
        if not (1 <= int(v) <= 12):
            raise ValueError("month out of range")
        return v

    @field_validator("year")
    @classmethod
    def _year_ok(cls, v: str) -> str:
        v = re.sub(r"\D", "", v)
        if len(v) not in (2, 4):
            raise ValueError("year must be 2 or 4 digits")
        return v


class CheckResult(BaseModel):
    status: str
    message: str
    card: dict
    step: int
    duration_ms: int


class BulkRequest(BaseModel):
    cards: List[CheckRequest]
    concurrency: int = Field(2, ge=1, le=10)


# ── Routes ────────────────────────────────────────────────────────
@app.get("/")
def root():
    return {
        "name": "mentos-autostripe",
        "target": BASE_URL,
        "auth": "enabled" if API_KEY else "disabled",
        "endpoints": ["POST /check", "POST /check/bulk", "GET /health"],
    }


@app.get("/health")
def health():
    return {"status": "ok", "timestamp": int(time.time())}


@app.post("/check", response_model=CheckResult, dependencies=[Depends(_auth)])
async def check(req: CheckRequest):
    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(
        _executor,
        check_card,
        req.card, req.month, req.year, req.cvv,
    )
    return result


@app.post("/check/bulk", dependencies=[Depends(_auth)])
async def check_bulk(req: BulkRequest):
    if len(req.cards) > MAX_BULK:
        raise HTTPException(status_code=413, detail=f"max {MAX_BULK} cards per request")

    loop = asyncio.get_event_loop()
    sem = asyncio.Semaphore(req.concurrency)

    async def _one(c: CheckRequest):
        async with sem:
            return await loop.run_in_executor(
                _executor,
                check_card,
                c.card, c.month, c.year, c.cvv,
            )

    results = await asyncio.gather(*[_one(c) for c in req.cards])
    approved = sum(1 for r in results if r["status"] == "approved")
    declined = sum(1 for r in results if r["status"] == "declined")
    errored = sum(1 for r in results if r["status"] == "error")

    return {
        "total": len(results),
        "approved": approved,
        "declined": declined,
        "error": errored,
        "results": results,
    }


@app.exception_handler(Exception)
async def _unhandled(request, exc):
    return JSONResponse(
        status_code=500,
        content={"status": "error", "message": f"internal: {exc!s}"},
    )
