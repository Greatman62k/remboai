from dataclasses import dataclass
from typing import Any


@dataclass
class Ctx:
    """What a handler needs. Built once at startup (or by a test)."""
    settings: Any
    pool: Any
    wa: Any
    llm: Any
    daraja: Any = None
    voice: Any = None
    vision: Any = None
