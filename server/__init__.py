"""Server-side building blocks for the Image Magic web application."""

from .database import Database, get_db
from .settings import Settings

__all__ = ["Database", "Settings", "get_db"]
