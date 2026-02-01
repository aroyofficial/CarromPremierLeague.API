from typing import Generic, TypeVar, Optional
from pydantic import BaseModel
from pydantic.generics import GenericModel

T = TypeVar("T")


class ApiResponse(GenericModel, Generic[T]):
    success: bool = True
    message: Optional[str] = None
    data: Optional[T] = None
