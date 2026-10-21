"""The grid of points over the city's land that travel times are routed to."""

from dataclasses import dataclass

import geopandas as gpd
import numpy as np
import numpy.typing as npt
import shapely

from subway_sheds import paths

WGS84 = "EPSG:4326"
# Web Mercator, which the map is drawn in, so each cell is a square on it
# and the page can draw a cell from just its column and row.
WEB_MERCATOR = "EPSG:3857"

# In Web Mercator meters, which are about 0.76 meters on the ground at NYC's latitude,
# so cells are about 190 meters across.
SPACING = 250


@dataclass(frozen=True)
class Grid:
    # The Web Mercator coordinates of cell (0, 0)'s center.
    x0: float
    y0: float
    # Each cell's column and row, ordered by row and then column; a cell's ID is its index.
    cols: npt.NDArray[np.int32]
    rows: npt.NDArray[np.int32]

    def points(self) -> gpd.GeoDataFrame:
        """Each cell's center, with its ID, as `r5py` takes destinations."""
        return gpd.GeoDataFrame(
            {"id": np.arange(len(self.cols))},
            geometry=shapely.points(self.x0 + self.cols * SPACING, self.y0 + self.rows * SPACING),
            crs=WEB_MERCATOR,
        ).to_crs(WGS84)


def land_grid() -> Grid:
    """The cells whose centers are on the city's land."""
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
