"""The streets R5 walks on, cut from Geofabrik's New York State extract."""

from pathlib import Path

import geopandas as gpd
import osmium
import osmium.osm

from subway_sheds import paths

# How far past NYC's land to keep streets, in degrees (about 2 km),
# so that walks near the edges, like across city lines, aren't cut short.
MARGIN = 0.02

# What R5 builds its walking network from, besides streets, sidewalks, footpaths, and steps,
# which are all `highway`.
PLATFORM_TAGS = {("railway", "platform"), ("public_transport", "platform")}


def _walkable(way: osmium.osm.Way) -> bool:
    return "highway" in way.tags or any(way.tags.get(key) == value for key, value in PLATFORM_TAGS)


def filter_streets(extract: Path, dest: Path) -> int:
    """
    Write the ways in `extract` that R5 walks on within NYC's bounds to `dest`,
    with the nodes they use. Returns how many ways were kept.
    """
    min_lon, min_lat, max_lon, max_lat = gpd.read_file(paths.SHORELINE).total_bounds
    min_lon -= MARGIN
    min_lat -= MARGIN
    max_lon += MARGIN
    max_lat += MARGIN

    def in_bounds(way: osmium.osm.Way) -> bool:
        return any(
            node.location.valid()
            and min_lon <= node.location.lon <= max_lon
            and min_lat <= node.location.lat <= max_lat
            for node in way.nodes
        )

    # `osmium` tells the format from the extension, so `.partial` goes before it.
    partial = dest.with_name(dest.name.replace(".osm.pbf", ".partial.osm.pbf"))
    kept = 0
    # Node tags are kept, for barriers, crossings, and the like.
    with osmium.BackReferenceWriter(
        partial, ref_src=extract, overwrite=True, remove_tags=False
    ) as w:
        # Nodes are read only for the locations of ways' nodes; `w` adds the ones it needs.
        processor = osmium.FileProcessor(extract, osmium.osm.NODE | osmium.osm.WAY)
        for obj in processor.with_locations():
            if isinstance(obj, osmium.osm.Way) and _walkable(obj) and in_bounds(obj):
                w.add_way(obj)
                kept += 1
    partial.rename(dest)
    return kept
