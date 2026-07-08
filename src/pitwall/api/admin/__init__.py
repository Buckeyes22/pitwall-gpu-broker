"""Admin API routes."""

from pitwall.api.admin import emergency, kill_switch
from pitwall.api.admin.audit_capability import router as audit_capability_router

__all__ = ["audit_capability_router", "emergency", "kill_switch"]
