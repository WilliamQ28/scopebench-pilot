"""Live scheduler settings; controls admission without killing active experiments."""

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast

RunMode = Literal["working", "idle", "high-memory", "drain"]


@dataclass(frozen=True)
class AdmissionSettings:
    """A validated profile; invalid input stops new admissions until corrected."""

    mode: str = "working"
    concurrency: int = 4
    budget_mib: int = 2048
    drain: bool = False
    error: str | None = None


def read_control(path: Path) -> AdmissionSettings:
    """Reload a small JSON control file; missing/malformed files fail closed for admission."""
    try:
        data = cast("object", json.loads(path.read_text()))
        if not isinstance(data, dict):
            raise TypeError("Control must be a JSON object")  # noqa: TRY301
        mode = cast("dict[str, object]", data).get("mode")
        if mode not in ("working", "idle", "high-memory", "drain"):
            raise ValueError("mode must be working, idle, high-memory, or drain")  # noqa: TRY301
        return AdmissionSettings(
            mode=cast("str", mode),
            concurrency=8 if mode in ("idle", "high-memory") else 4,
            budget_mib=8192 if mode == "high-memory" else 3072 if mode == "idle" else 2048,
            drain=mode == "drain",
        )
    except (OSError, ValueError, TypeError) as exc:
        return AdmissionSettings(mode="invalid", concurrency=0, error=str(exc))


def write_control(path: Path, mode: str) -> None:
    """Atomically switch between fixed safe profiles, leaving current tasks untouched."""
    if mode not in ("working", "idle", "high-memory", "drain"):
        raise ValueError("Unknown run mode")
    temporary = path.with_suffix(".tmp")
    _ = temporary.write_text(json.dumps({"mode": mode}, indent=2) + "\n")
    _ = temporary.replace(path)
