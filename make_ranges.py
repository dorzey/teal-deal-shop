#!/usr/bin/env python3
"""Generates public/ranges.json: the data behind the storefront's "Ducks near …" filter.

Three things, all deliberately rough — a toy, not a field guide:
  regions  coarse hand-drawn polygons (WKT, lon/lat) for a few parts of the world
  cities   the places a visitor can pick, each with the region it should fall in
  ducks    for every species in 01_seed.sql, the regions where it is found at some
           time of year

The browser loads the `spatial` extension and answers "which ducks live where I am?"
with ST_Contains(region, ST_Point(lon, lat)).

    python3 make_ranges.py
"""
import json
import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))

REGIONS = {
    "North America": "POLYGON((-168 66, -141 70, -95 72, -62 60, -52 47, -70 42, -81 25, -80 9, -92 14, -106 20, -118 31, -125 42, -130 55, -168 66))",
    "South America": "POLYGON((-80 1, -72 12, -60 10, -50 0, -35 -6, -40 -22, -53 -34, -62 -40, -68 -55, -75 -50, -72 -30, -81 -6, -80 1))",
    "Europe": "POLYGON((-12 36, -10 44, -5 50, -8 58, 5 71, 30 71, 45 60, 45 41, 28 36, -12 36))",
    "Africa": "POLYGON((-18 15, -17 21, -6 36, 10 37, 33 31, 43 12, 51 12, 40 -15, 35 -25, 27 -34, 18 -35, 12 -18, 9 4, -8 4, -18 15))",
    "Asia": "POLYGON((45 41, 45 60, 60 72, 110 78, 180 70, 180 52, 150 46, 143 32, 130 30, 122 22, 108 8, 98 6, 90 22, 78 8, 66 22, 55 26, 45 30, 45 41))",
    "Australasia": "MULTIPOLYGON(((112 -22, 122 -17, 136 -12, 142 -10, 154 -26, 153 -38, 146 -44, 138 -35, 115 -35, 112 -22)), ((166 -34, 179 -34, 179 -47, 166 -47, 166 -34)))",
    "Hawaii": "POLYGON((-161 18, -154 18, -154 23, -161 23, -161 18))",
}

# name, lon, lat, region it should fall in
CITIES = [
    ("Amsterdam", 4.90, 52.37, "Europe"), ("London", -0.13, 51.51, "Europe"),
    ("Paris", 2.35, 48.86, "Europe"), ("Berlin", 13.40, 52.52, "Europe"),
    ("New York", -74.01, 40.71, "North America"), ("Chicago", -87.63, 41.88, "North America"),
    ("Seattle", -122.33, 47.61, "North America"), ("Mexico City", -99.13, 19.43, "North America"),
    ("São Paulo", -46.63, -23.55, "South America"), ("Buenos Aires", -58.38, -34.60, "South America"),
    ("Cape Town", 18.42, -33.92, "Africa"), ("Nairobi", 36.82, -1.29, "Africa"),
    ("Delhi", 77.21, 28.61, "Asia"), ("Tokyo", 139.69, 35.69, "Asia"),
    ("Sydney", 151.21, -33.87, "Australasia"), ("Auckland", 174.76, -36.85, "Australasia"),
    ("Honolulu", -157.86, 21.31, "Hawaii"),
]

NA, SA, EU, AF, AS, AU, HI = "North America", "South America", "Europe", "Africa", "Asia", "Australasia", "Hawaii"
DUCKS = {
    # dabbling
    "Mallard": [NA, EU, AS, AF], "Northern Pintail": [NA, EU, AS, AF], "Green-winged Teal": [NA, EU, AS, AF],
    "Blue-winged Teal": [NA, SA], "Cinnamon Teal": [NA, SA], "Gadwall": [NA, EU, AS, AF],
    "Eurasian Wigeon": [EU, AS, AF], "American Wigeon": [NA], "Northern Shoveler": [NA, EU, AS, AF],
    "American Black Duck": [NA], "Garganey": [EU, AS, AF], "Falcated Duck": [AS],
    "Mottled Duck": [NA], "Gray Teal": [AU], "Pacific Black Duck": [AU], "Cape Teal": [AF],
    "Chestnut Teal": [AU], "Marbled Teal": [EU, AF, AS], "Laysan Duck": [HI], "Hawaiian Duck": [HI],
    "Yellow-billed Duck": [AF], "Spot-billed Duck": [AS],
    # diving
    "Canvasback": [NA], "Redhead": [NA], "Ring-necked Duck": [NA], "Greater Scaup": [NA, EU, AS],
    "Lesser Scaup": [NA], "Barrow's Goldeneye": [NA], "Tufted Duck": [EU, AS, AF], "Common Pochard": [EU, AS, AF],
    "Ferruginous Duck": [EU, AS, AF], "Southern Pochard": [SA, AF], "Common Goldeneye": [NA, EU, AS],
    "Bufflehead": [NA], "Hooded Merganser": [NA], "Common Merganser": [NA, EU, AS],
    "Red-breasted Merganser": [NA, EU, AS], "Smew": [EU, AS], "Ruddy Duck": [NA, SA],
    # sea
    "Common Eider": [NA, EU, AS], "King Eider": [NA, EU, AS], "Spectacled Eider": [NA, AS], "Steller's Eider": [AS, NA, EU],
    "Long-tailed Duck": [NA, EU, AS], "Harlequin Duck": [NA, AS], "Surf Scoter": [NA],
    "White-winged Scoter": [NA, AS], "Black Scoter": [NA, AS], "Velvet Scoter": [EU, AS],
    "Common Scoter": [EU, AS],
    # perching
    "Wood Duck": [NA], "Mandarin Duck": [AS, EU], "Muscovy Duck": [SA, NA], "Comb Duck": [AF, AS, SA],
    "Maned Duck": [AU], "Hartlaub's Duck": [AF], "Brazilian Teal": [SA], "Fulvous Whistling Duck": [NA, SA, AF, AS],
    "White-faced Whistling Duck": [SA, AF], "Black-bellied Whistling Duck": [NA, SA],
}


def seed_species():
    with open(os.path.join(HERE, "01_seed.sql")) as f:
        names = re.findall(r"^\s*\('((?:[^']|'')+)', '(?:dabbling|diving|sea|perching)',", f.read(), flags=re.M)
    return [n.replace("''", "'") for n in names]   # SQL-escaped quotes


def main():
    species = seed_species()
    assert len(species) == 60, len(species)
    assert set(species) == set(DUCKS), (set(species) ^ set(DUCKS))
    for name, regions in DUCKS.items():
        assert regions and set(regions) <= set(REGIONS), name
    assert {c[3] for c in CITIES} <= set(REGIONS)
    out = {
        "note": "Rough, hand-drawn ranges for fun. Not a field guide.",
        "regions": REGIONS,
        "cities": [{"name": n, "lon": lon, "lat": lat, "region": r} for n, lon, lat, r in CITIES],
        "ducks": DUCKS,
    }
    with open(os.path.join(HERE, "public", "ranges.json"), "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
        f.write("\n")
    print(f"public/ranges.json: {len(REGIONS)} regions, {len(CITIES)} cities, {len(DUCKS)} ducks")


if __name__ == "__main__":
    main()
