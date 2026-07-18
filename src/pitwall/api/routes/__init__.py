"""FastAPI route handlers."""

from pitwall.api.routes.inference import router as inference_router
from pitwall.api.routes.jobs import router as jobs_router
from pitwall.api.routes.leases import router as lease_router
from pitwall.api.routes.openai import router as openai_router
from pitwall.api.routes.webhook_subscriptions import router as webhook_subscription_router

__all__ = [
    "inference_router",
    "jobs_router",
    "lease_router",
    "openai_router",
    "webhook_subscription_router",
]
