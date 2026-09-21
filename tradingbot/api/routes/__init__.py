from fastapi import APIRouter

from tradingbot.api.routes import account, health, market, positions, risk, signals, trades

router = APIRouter()
router.include_router(health.router)
router.include_router(account.router, prefix="/api/v1", tags=["account"])
router.include_router(market.router, prefix="/api/v1", tags=["market"])
router.include_router(positions.router, prefix="/api/v1", tags=["positions"])
router.include_router(signals.router, prefix="/api/v1", tags=["signals"])
router.include_router(trades.router, prefix="/api/v1", tags=["trades"])
router.include_router(risk.router, prefix="/api/v1", tags=["risk"])

__all__ = ["router"]
