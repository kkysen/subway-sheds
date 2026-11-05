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
