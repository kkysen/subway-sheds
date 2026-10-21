from enum import StrEnum
from pathlib import Path

from subway_sheds import paths


class Scenario(StrEnum):
    # Today's subway, from MTA's published static GTFS.
    BASELINE = "baseline"
    # The whole subway with QueensLink's M added, from the QueensLink scenario feed.
    QUEENSLINK = "queenslink"

    @property
    def gtfs(self) -> Path:
        return paths.GTFS / f"{self}.zip"
