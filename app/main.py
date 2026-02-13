import logging

from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.responses import JSONResponse

from app.middleware.apix import ApixMiddleware
from app.middleware.rate_limit import RateLimitMiddleware
from app.routes.position_receipt import router as position_receipt_router
from app.services.rpc import close_client, get_client
from app.config import BASE_RPC_URL, SOLANA_RPC_URL

# --- Logging ---
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(name)s | %(levelname)s | %(message)s",
)
logger = logging.getLogger("apix")


# --- Lifespan ---
@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Position Receipt API starting up")
    yield
    logger.info("Shutting down — closing HTTP client")
    await close_client()


# --- App ---
app = FastAPI(
    title="Position Receipt API",
    description="Verify a wallet's current position in any token — the 'show me your receipts' primitive.",
    version="0.1.0",
    lifespan=lifespan,
)

# Middleware stack (outermost first):
# 1. Rate limiting — reject before doing any work
# 2. APIX middleware — logging + body unwrapping
app.add_middleware(ApixMiddleware)
app.add_middleware(RateLimitMiddleware)

# Routes
app.include_router(position_receipt_router)


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/health/ready")
async def health_ready():
    checks = {}
    client = get_client()

    for name, url, method, params in [
        ("base_rpc", BASE_RPC_URL, "eth_blockNumber", []),
        ("solana_rpc", SOLANA_RPC_URL, "getHealth", []),
    ]:
        try:
            resp = await client.post(
                url,
                json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
                timeout=3.0,
            )
            data = resp.json()
            checks[name] = "ok" if "result" in data else "error"
        except Exception:
            checks[name] = "unreachable"

    all_ok = all(v == "ok" for v in checks.values())
    status_code = 200 if all_ok else 503
    return JSONResponse(
        status_code=status_code,
        content={"status": "ok" if all_ok else "degraded", "checks": checks},
    )
