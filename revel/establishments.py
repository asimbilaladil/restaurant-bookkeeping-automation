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

DEFAULT_ESTABLISHMENTS: list[int] = [est_id for est_id, _ in ESTABLISHMENTS]
ESTABLISHMENT_NAMES: dict[int, str] = {est_id: name for est_id, name in ESTABLISHMENTS}
