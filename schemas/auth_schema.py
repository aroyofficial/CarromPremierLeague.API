from pydantic import BaseModel, Field


class LoginRequest(BaseModel):
    username: str = Field(..., min_length=3, max_length=100)
    password: str = Field(..., min_length=12, max_length=256)


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int


class AdminCreateRequest(BaseModel):
    username: str = Field(..., min_length=3, max_length=100, pattern=r"^[A-Za-z0-9_.-]+$")
    password: str = Field(..., min_length=12, max_length=256)


class AdminUpdateRequest(BaseModel):
    username: str | None = Field(None, min_length=3, max_length=100, pattern=r"^[A-Za-z0-9_.-]+$")
    password: str | None = Field(None, min_length=12, max_length=256)
    is_active: bool | None = None


class AdminResponse(BaseModel):
    id: int
    username: str
    is_active: bool
