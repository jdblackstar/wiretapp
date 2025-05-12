from datetime import datetime
from typing import Any

from pydantic import BaseModel


class TelemetryEvent(BaseModel):
    event_id: str
    timestamp: datetime
    app_name: str
    event_type: str  # e.g., 'openai_call', 'user_login', etc.
    session_id: str | None = None  # Hashed
    user_id: str | None = None  # Hashed
    # We can make the payload more specific later, or keep it flexible
    payload: dict[str, Any]
