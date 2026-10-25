"""Live admission is mutable; the experiment and running units remain fixed."""

from pathlib import Path

import pytest

from scopebench.admission import AdmissionSettings, RunMode, read_control, write_control


@pytest.mark.parametrize(
    ("mode", "slots", "budget"),
    [("working", 4, 2048), ("idle", 12, 6144), ("high-memory", 12, 8192), ("drain", 4, 2048)],
)
def test_profiles(tmp_path: Path, mode: RunMode, slots: int, budget: int) -> None:
    path = tmp_path / "control.json"
    write_control(path, mode)
    settings = read_control(path)
    assert settings.concurrency == slots
    assert settings.budget_mib == budget
    assert settings.drain is (mode == "drain")
    assert not path.with_suffix(".tmp").exists()


@pytest.mark.parametrize("content", [None, "{", "[]", '{"mode":"bad"}'])
def test_invalid_control_stops_admission(tmp_path: Path, content: str | None) -> None:
    path = tmp_path / "control.json"
    if content is not None:
        path.write_text(content)
    settings = read_control(path)
    assert settings.error
    assert settings.concurrency == 0
    assert settings.drain is False


def test_bad_write_and_default() -> None:
    assert AdmissionSettings().concurrency == 4
    with pytest.raises(ValueError, match="Unknown"):
        write_control(Path("unused"), "bad")
