import requests, subprocess, os
from pathlib import Path

BASE = "https://ckan0.cf.opendata.inter.prod-toronto.ca"
DATASET_ID = "zoning-by-law"

# Known GeoJSON resource names from the dataset
LAYERS = [
    ("zoning-area-4326.geojson", "zoning_area"),
    ("zoning-height-overlay-4326.geojson", "height_overlay"),
    ("zoning-lot-coverage-overlay-4326.geojson", "lot_coverage_overlay"),
    ("zoning-parking-zone-overlay-4326.geojson", "parking_zone_overlay"),
    ("zoning-policy-area-overlay-4326.geojson", "policy_area_overlay"),
    ("zoning-building-setback-overlay-4326.geojson", "building_setback_overlay"),
    ("zoning-rooming-house-overlay-4326.geojson", "rooming_house_overlay"),
    ("zoning-priority-retail-streets-overlay-4326.geojson", "priority_retail_overlay"),
]

DB = "postgresql://user:pass@localhost:5433/toronto_zoning"

def get_all_resources():
    """Fetch all resource URLs from the CKAN dataset."""
    url = f"{BASE}/api/3/action/package_show"
    data = requests.get(url, params={"id": DATASET_ID}).json()
    resources = data["result"]["resources"]
    # Build name → url map
    return {r["name"]: r["url"] for r in resources if r["format"] == "GeoJSON"}

def download_and_load(resource_url, table_name):
    """Download GeoJSON and load into PostGIS using ogr2ogr."""
    print(f"Loading {table_name}...")
    subprocess.run([
        "ogr2ogr",
        "-f", "PostgreSQL",
        f"PG:{DB}",
        f"/vsicurl/{resource_url}",   # stream directly, no disk save
        "-nln", table_name,           # table name in PostGIS
        "-overwrite",
        "-t_srs", "EPSG:4326",        # keep WGS84
        "-lco", "GEOMETRY_NAME=geom",
        "-lco", "FID=id",
    ], check=True)
    # Add spatial index
    subprocess.run([
        "psql", DB, "-c",
        f"CREATE INDEX IF NOT EXISTS {table_name}_geom_idx "
        f"ON {table_name} USING GIST(geom);"
    ])
    print(f"  Done: {table_name}")

def load_local(file_path, table_name):
    print(f"Loading {table_name} from {file_path}...")

    subprocess.run([
        "ogr2ogr",
        "-f", "PostgreSQL",
        f"PG:{DB}",
        str(file_path),             
        "-nln", table_name,
        "-overwrite",
        "-t_srs", "EPSG:4326",
        "-lco", "GEOMETRY_NAME=geom",
        "-lco", "FID=id",
    ], check=True)

    subprocess.run([
        "psql", DB, "-c",
        f"CREATE INDEX IF NOT EXISTS {table_name}_geom_idx "
        f"ON {table_name} USING GIST(geom);"
    ])

    print(f"  Done: {table_name}")
    
def main():
    base_path = Path(__file__).parent.parent

    for filename, table in LAYERS:
        file_path = base_path / filename

        if os.path.exists(file_path):
            load_local(file_path, table)
        else:
            print(f"❌ File not found: {file_path}")
if __name__ == "__main__":
    main()
