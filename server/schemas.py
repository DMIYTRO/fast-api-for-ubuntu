"""HTTP request and response schemas."""

from typing import Literal

from pydantic import BaseModel, Field

from config.profiles import DEFAULT_DIRECTION


class LoginRequest(BaseModel):
    password: str = Field(min_length=1, max_length=1024)


class SessionResponse(BaseModel):
    authenticated: bool
    expires_at: str | None = None


class ErrorBody(BaseModel):
    code: str
    message: str


class ErrorEnvelope(BaseModel):
    error: ErrorBody


class CheckOptions(BaseModel):
    input_path: str
    direction: str = DEFAULT_DIRECTION
    approve_corrections: bool = False
    correction_policy: Literal["ask", "auto", "reject"] = "ask"
    create_pdfs: bool = True
    generate_previews: bool = True
    copy_failures: bool = True


class CorrectionRequest(BaseModel):
    decision: Literal["confirm", "reject"]


class OrderActionRequest(BaseModel):
    order_ids: list[str] = Field(min_length=1)
    run_id: str | None = None
    comment: str | None = None
    design: bool = True
    design_cost: str = "0"
    conflict_strategy: Literal["fail", "replace", "rename"] = "fail"
