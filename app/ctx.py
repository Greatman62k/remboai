from dataclasses import dataclass
from typing import Any


@dataclass
class Ctx:
    settings: Any
    pool: Any
    wa: Any
    llm: Any
    redis: Any = None
    daraja: Any = None
    voice: Any = None
    vision: Any = None
