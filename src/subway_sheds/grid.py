"""The grid of points over the city's land that travel times are routed to."""

from dataclasses import dataclass
from pathlib import Path

import geopandas as gpd
import numpy as np
import numpy.typing as npt
import shapely
from pyproj import Transformer

from subway_sheds import paths

WGS84 = "EPSG:4326"
# Web Mercator, which the map is drawn in, so each cell is a square on it
# and the page can draw a cell from just its column and row.
WEB_MERCATOR = "EPSG:3857"

# In Web Mercator meters, which are about 0.76 meters on the ground at NYC's latitude,
# so cells are about 190 meters across.
SPACING = 250

# How far, on the ground, a cell's center can be from a street routing would link it to
# for the cell to count: well past a cell's half-diagonal, about 135 meters,
# so that parks and the like with paths through them count,
# but marsh islands in Jamaica Bay, with none, don't.
# Routing would otherwise link them to a street up to 1600 m away, and give them times.
MAX_STREET_DISTANCE = 250


@dataclass(frozen=True)
class Grid:
    # The Web Mercator coordinates of cell (0, 0)'s center.
    x0: float
    y0: float
    # Each cell's column and row, ordered by row and then column; a cell's ID is its index.
    cols: npt.NDArray[np.int32]
    rows: npt.NDArray[np.int32]

    def points(self) -> gpd.GeoDataFrame:
        """Each cell's center, with its ID."""
        return gpd.GeoDataFrame(
            {"id": np.arange(len(self.cols))},
            geometry=shapely.points(self.x0 + self.cols * SPACING, self.y0 + self.rows * SPACING),
            crs=WEB_MERCATOR,
        ).to_crs(WGS84)


def on_land() -> Grid:
    """The cells whose centers are on the city's land, near the streets or not."""
    land = gpd.read_file(paths.SHORELINE).to_crs(WEB_MERCATOR).union_all()
    min_x, min_y, max_x, max_y = land.bounds
    # Line cells up with multiples of the spacing, so the grid doesn't move if the land does.
    x0 = np.floor(min_x / SPACING) * SPACING
    y0 = np.floor(min_y / SPACING) * SPACING
    cols, rows = np.meshgrid(
        np.arange(int((max_x - x0) / SPACING) + 1, dtype=np.int32),
        np.arange(int((max_y - y0) / SPACING) + 1, dtype=np.int32),
    )
    cols = cols.ravel()
    rows = rows.ravel()
    on_land = shapely.contains_xy(land, x0 + cols * SPACING, y0 + rows * SPACING)
    return Grid(x0=float(x0), y0=float(y0), cols=cols[on_land], rows=rows[on_land])


def lat_lons(grid: Grid) -> list[tuple[float, float]]:
    """Each of `grid`'s cells' center, as `(latitude, longitude)`."""
    to_wgs84 = Transformer.from_crs(WEB_MERCATOR, WGS84, always_xy=True)
    lons, lats = to_wgs84.transform(grid.x0 + grid.cols * SPACING, grid.y0 + grid.rows * SPACING)
    return list(zip(lats.tolist(), lons.tolist(), strict=True))


def write(grid: Grid, dest: Path) -> None:
    np.savez(dest, origin=np.array([grid.x0, grid.y0]), cols=grid.cols, rows=grid.rows)


def land_grid() -> Grid:
    """
    The cells whose centers are on the city's land, near something to walk on,
    as `street_walks` last found them.
    """
    with np.load(paths.GRID) as saved:
        x0, y0 = saved["origin"].tolist()
        return Grid(x0=x0, y0=y0, cols=saved["cols"], rows=saved["rows"])
