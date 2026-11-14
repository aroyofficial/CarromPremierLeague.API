from typing import Optional

from pydantic import BaseModel


class MediaDefaultsResponse(BaseModel):
    player: Optional[str] = None
    team: Optional[str] = None
    uploads_enabled: bool = False
