from pydantic import BaseModel, Field
from typing import Optional


class CountryBase(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    iso_code2: str = Field(..., min_length=2, max_length=2)
    iso_code3: str = Field(..., min_length=3, max_length=3)
    capital: Optional[str] = Field(None, max_length=255)
    phone_code: Optional[str] = Field(None, max_length=20)
    continent: Optional[str] = Field(None, max_length=255)


class CountryCreateRequest(CountryBase):
    pass


class CountryUpdateRequest(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=255)
    iso_code2: Optional[str] = Field(None, min_length=2, max_length=2)
    iso_code3: Optional[str] = Field(None, min_length=3, max_length=3)
    capital: Optional[str] = Field(None, max_length=255)
    phone_code: Optional[str] = Field(None, max_length=20)
    continent: Optional[str] = Field(None, max_length=255)


class CountryResponse(CountryBase):
    id: int