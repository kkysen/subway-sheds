import json
from pathlib import Path

import osmium
import osmium.osm
import pytest

from subway_sheds import osm, paths


def test_filter_streets_keeps_walkable_ways_in_nyc(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A square of "land" around Broad Channel.
    shoreline = tmp_path / "shoreline.json"
    shoreline.write_text(
        json.dumps(
            {
                "type": "Feature",
                "properties": {},
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [
                        [[-73.83, 40.60], [-73.80, 40.60], [-73.80, 40.62], [-73.83, 40.60]]
                    ],
                },
            }
        )
    )
    monkeypatch.setattr(paths, "SHORELINE", shoreline)

    extract = tmp_path / "extract.osm.pbf"
    with osmium.SimpleWriter(extract) as writer:
        nodes = {
            1: (-73.815, 40.608),
            2: (-73.814, 40.609),
            # Albany.
            3: (-73.75, 42.65),
            4: (-73.76, 42.66),
        }
        for node_id, location in nodes.items():
            writer.add_node(osmium.osm.mutable.Node(id=node_id, location=location))
        ways = [
            (10, [1, 2], {"highway": "footway"}),
            (11, [1, 2], {"building": "yes"}),
            (12, [3, 4], {"highway": "residential"}),
            (13, [2, 1], {"railway": "platform"}),
        ]
        for way_id, refs, tags in ways:
            writer.add_way(osmium.osm.mutable.Way(id=way_id, nodes=refs, tags=tags))

    dest = tmp_path / "streets.osm.pbf"
    assert osm.filter_streets(extract, dest) == 2

    kept = {obj.id: type(obj) for obj in osmium.FileProcessor(dest)}
    assert kept == {
        1: osmium.osm.Node,
        2: osmium.osm.Node,
        10: osmium.osm.Way,
        13: osmium.osm.Way,
    }
