ESTABLISHMENTS: list[tuple[int, str]] = [
    (32, "LCF Airtex"),
    (14, "LCF Beaumont"),
    (48, "LCF Downtown Houston"),
    (7,  "LCF Ella"),
    (6,  "LCF Katy"),
    (25, "LCF Mission Bend"),
    (36, "LCF Missouri City"),
    (26, "LCF Nederland"),
    (20, "LCF Pasadena"),
    (40, "LCF Rosenberg"),
    (15, "LCF Shepherd"),
]

# R365 DSS grid uses different names for some locations.
# Maps establishment ID → name as it appears in the R365 DSS grid.
R365_NAME_OVERRIDES: dict[int, str] = {
    48: "LCF Downtown",
    7:  "LCF Garden Oaks",
    20: "LCF Fairmont",
}

DEFAULT_ESTABLISHMENTS: list[int] = [est_id for est_id, _ in ESTABLISHMENTS]
ESTABLISHMENT_NAMES: dict[int, str] = {est_id: name for est_id, name in ESTABLISHMENTS}
