from pathlib import Path

import pytest

from subway_sheds import inputs, paths


def test_modules_follow_imports_indirectly() -> None:
    # `scenarios` imports `fit` and `timetable`, which import `_route` and `gtfs`.
    assert {"scenarios", "fit", "timetable", "gtfs", "_route"} <= inputs.modules("scenarios")
    assert "export" not in inputs.modules("scenarios")


def test_files_are_what_scenarios_read() -> None:
    files = inputs.files("scenarios")
    assert paths.QUEENSLINK_RUN_TIMES in files
    assert inputs.ROUTE / "src" / "sat.rs" in files
    assert inputs.PACKAGE / "export.py" not in files


def test_made_until_written_again(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(paths, "ROOT", tmp_path)
    marker = tmp_path / "inputs.sha256"
    out = tmp_path / "out.bin"
    assert not inputs.made(marker, "a", [out])
    out.write_bytes(b"made")
    inputs.mark(marker, "a", [out])
    assert inputs.made(marker, "a", [out])
    assert not inputs.made(marker, "b", [out])
    # As an older checkout writes it, without knowing the marker.
    out.write_bytes(b"made again")
    assert not inputs.made(marker, "a", [out])
