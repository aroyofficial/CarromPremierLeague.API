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
    permissions: list[str] = Field(default_factory=list, max_length=100)


class AdminUpdateRequest(BaseModel):
    username: str | None = Field(None, min_length=3, max_length=100, pattern=r"^[A-Za-z0-9_.-]+$")
    password: str | None = Field(None, min_length=12, max_length=256)
    is_active: bool | None = None


class AdminPermissionsUpdate(BaseModel):
    permissions: list[str] = Field(..., max_length=100)


class AdminResponse(BaseModel):
    id: int
    username: str
    is_active: bool
    permissions: list[str] = Field(default_factory=list)


class PermissionResponse(BaseModel):
    code: str
    area: str
    name: str
    description: str
    area_code: str
    parent: str | None = None


class PermissionNode(BaseModel):
    code: str
    name: str
    description: str
    parent: str | None = None


class PermissionAreaNode(BaseModel):
    code: str
    name: str
    description: str
    icon: str
    permissions: list[PermissionNode]


class PermissionSectionNode(BaseModel):
    code: str
    name: str
    description: str
    areas: list[PermissionAreaNode]


class PermissionRoleResponse(BaseModel):
    code: str
    name: str
    description: str
    permissions: list[str]


class PermissionTreeResponse(BaseModel):
    sections: list[PermissionSectionNode]
    roles: list[PermissionRoleResponse]
