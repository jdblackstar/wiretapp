from pydantic import BaseModel
from typing import Dict, Any, Optional
from datetime import datetime


class TelemetryEvent(BaseModel):
    event_id: str
    timestamp: datetime
    app_name: str
    event_type: str  # e.g., 'openai_call', 'user_login', etc.
    session_id: Optional[str] = None  # Hashed
    user_id: Optional[str] = None  # Hashed
    # We can make the payload more specific later, or keep it flexible
    payload: Dict[str, Any]
