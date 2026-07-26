"""Password authentication and server-side session support for FastAPI."""

from __future__ import annotations

from collections import defaultdict, deque
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
import hashlib
import secrets
import threading
import time

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from .database import get_db
from .errors import APIError
from .models import SessionRecord
from .schemas import LoginRequest, SessionResponse
from .settings import Settings


AUTH_ERROR_MESSAGE = "Неверный пароль или слишком много попыток"
UNAUTHENTICATED_MESSAGE = "Требуется авторизация"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def token_digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def password_fingerprint(password_hash: str) -> str:
    return hashlib.sha256(password_hash.encode("utf-8")).hexdigest()


class LoginRateLimiter:
    """Small process-local limiter suitable for the single-server first stage."""

    def __init__(self, max_attempts: int, window_seconds: int):
        self.max_attempts = max_attempts
        self.window_seconds = window_seconds
        self._failures: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def _prune(self, failures: deque[float], now: float) -> None:
        threshold = now - self.window_seconds
        while failures and failures[0] <= threshold:
            failures.popleft()

    def is_limited(self, key: str, now: float | None = None) -> bool:
        timestamp = time.monotonic() if now is None else now
        with self._lock:
            failures = self._failures[key]
            self._prune(failures, timestamp)
            return len(failures) >= self.max_attempts

    def record_failure(self, key: str, now: float | None = None) -> None:
        timestamp = time.monotonic() if now is None else now
        with self._lock:
            failures = self._failures[key]
            self._prune(failures, timestamp)
            failures.append(timestamp)

    def reset(self, key: str) -> None:
        with self._lock:
            self._failures.pop(key, None)


class AuthService:
    def __init__(
        self,
        settings: Settings,
        *,
        password_hasher: PasswordHasher | None = None,
        password_hash_provider: Callable[[], str | None] | None = None,
        clock: Callable[[], datetime] = _utcnow,
        sleeper: Callable[[float], None] = time.sleep,
    ):
        self.settings = settings
        self.password_hasher = password_hasher or PasswordHasher()
        self._password_hash_provider = (
            password_hash_provider or (lambda: self.settings.password_hash)
        )
        self.clock = clock
        self.sleeper = sleeper
        self.rate_limiter = LoginRateLimiter(
            settings.login_attempts, settings.login_window_seconds
        )

    def current_password_hash(self) -> str | None:
        return self._password_hash_provider()

    def authenticate(self, db: Session, password: str, client_key: str) -> tuple[str, SessionRecord]:
        configured_hash = self.current_password_hash()
        limited = self.rate_limiter.is_limited(client_key)
        valid = False
        if configured_hash and not limited:
            try:
                valid = self.password_hasher.verify(configured_hash, password)
            except (InvalidHashError, VerificationError, VerifyMismatchError):
                valid = False

        if not valid:
            self.rate_limiter.record_failure(client_key)
            self.sleeper(self.settings.login_failure_delay_seconds)
            raise APIError(401, "invalid_credentials", AUTH_ERROR_MESSAGE)

        self.rate_limiter.reset(client_key)
        now = self.clock()
        fingerprint = password_fingerprint(configured_hash)
        db.execute(
            update(SessionRecord)
            .where(
                SessionRecord.revoked_at.is_(None),
                SessionRecord.password_fingerprint != fingerprint,
            )
            .values(revoked_at=now)
        )
        raw_token = secrets.token_urlsafe(32)
        record = SessionRecord(
            token_hash=token_digest(raw_token),
            password_fingerprint=fingerprint,
            created_at=now,
            last_seen_at=now,
            expires_at=now + timedelta(hours=self.settings.session_hours),
        )
        db.add(record)
        db.flush()
        return raw_token, record

    def validate(self, db: Session, raw_token: str | None) -> SessionRecord | None:
        if not raw_token:
            return None
        record = db.scalar(
            select(SessionRecord).where(
                SessionRecord.token_hash == token_digest(raw_token)
            )
        )
        if record is None or record.revoked_at is not None:
            return None

        now = self.clock()
        configured_hash = self.current_password_hash()
        current_fingerprint = (
            password_fingerprint(configured_hash) if configured_hash else None
        )
        if (
            _as_utc(record.expires_at) <= now
            or record.password_fingerprint != current_fingerprint
        ):
            record.revoked_at = now
            # Authentication dependencies raise immediately after this result;
            # persist revocation before their unit of work is rolled back.
            db.commit()
            return None

        record.last_seen_at = now
        db.flush()
        return record

    def revoke(self, db: Session, raw_token: str | None) -> None:
        if not raw_token:
            return
        db.execute(
            update(SessionRecord)
            .where(
                SessionRecord.token_hash == token_digest(raw_token),
                SessionRecord.revoked_at.is_(None),
            )
            .values(revoked_at=self.clock())
        )

    def revoke_all(self, db: Session) -> int:
        result = db.execute(
            update(SessionRecord)
            .where(SessionRecord.revoked_at.is_(None))
            .values(revoked_at=self.clock())
        )
        return result.rowcount or 0


def _client_key(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def create_auth_router(
    settings: Settings | None = None, service: AuthService | None = None
) -> APIRouter:
    settings = settings or Settings.from_env()
    service = service or AuthService(settings)
    router = APIRouter(prefix="/api/auth", tags=["auth"])

    @router.post("/login", response_model=SessionResponse)
    def login(
        payload: LoginRequest,
        request: Request,
        response: Response,
        db: Session = Depends(get_db),
    ) -> SessionResponse:
        raw_token, record = service.authenticate(
            db, payload.password, _client_key(request)
        )
        secure = (
            settings.cookie_secure
            if settings.cookie_secure is not None
            else request.url.scheme == "https"
        )
        response.set_cookie(
            key=settings.cookie_name,
            value=raw_token,
            max_age=settings.session_hours * 60 * 60,
            httponly=True,
            secure=secure,
            samesite="strict",
            path="/",
        )
        return SessionResponse(
            authenticated=True, expires_at=record.expires_at.isoformat()
        )

    @router.post("/logout", status_code=204)
    def logout(
        request: Request,
        response: Response,
        db: Session = Depends(get_db),
    ) -> None:
        service.revoke(db, request.cookies.get(settings.cookie_name))
        response.delete_cookie(
            settings.cookie_name,
            path="/",
            httponly=True,
            secure=(
                settings.cookie_secure
                if settings.cookie_secure is not None
                else request.url.scheme == "https"
            ),
            samesite="strict",
        )

    @router.get("/session", response_model=SessionResponse)
    def session_status(
        request: Request, db: Session = Depends(get_db)
    ) -> SessionResponse:
        record = service.validate(db, request.cookies.get(settings.cookie_name))
        if record is None:
            raise APIError(401, "authentication_required", UNAUTHENTICATED_MESSAGE)
        return SessionResponse(
            authenticated=True, expires_at=record.expires_at.isoformat()
        )

    return router


def require_session(
    settings: Settings | None = None, service: AuthService | None = None
):
    """Build a dependency reusable on every protected API/file route."""
    settings = settings or Settings.from_env()
    service = service or AuthService(settings)

    def dependency(
        request: Request, db: Session = Depends(get_db)
    ) -> SessionRecord:
        record = service.validate(db, request.cookies.get(settings.cookie_name))
        if record is None:
            raise APIError(401, "authentication_required", UNAUTHENTICATED_MESSAGE)
        # Release SQLite's write lock before the protected endpoint opens its
        # own repository transaction (for example when creating a check run).
        db.commit()
        return record

    return dependency
