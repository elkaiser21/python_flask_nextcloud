import xml.etree.ElementTree as ET
from pathlib import Path
from datetime import datetime
import shutil

GPX_EXT = ".gpx"
TARGET_ROOT = Path("C:/MyWebOkeCloud/MyTracksSync")

def extract_gpx_datetime(gpx_file):
    tree = ET.parse(gpx_file)
    root = tree.getroot()

    # GPX namespace handling
    ns = {"gpx": root.tag.split("}")[0].strip("{")}

    times = []

    # 1) track points (most reliable)
    for time_elem in root.findall(".//gpx:trkpt/gpx:time", ns):
        times.append(time_elem.text)

    # 2) track-level time
    for time_elem in root.findall(".//gpx:trk/gpx:time", ns):
        times.append(time_elem.text)

    # 3) metadata time
    for time_elem in root.findall(".//gpx:metadata/gpx:time", ns):
        times.append(time_elem.text)

    if not times:
        return None

    # parse ISO 8601 timestamps
    datetimes = [
        datetime.fromisoformat(t.replace("Z", "+00:00"))
        for t in times
    ]

    return min(datetimes)

def organize_gpx_files(source_dir):
    for gpx_file in source_dir.rglob(f"*{GPX_EXT}"):
        try:
            dt = extract_gpx_datetime(gpx_file)
            if dt is None:
                print(f"⚠️ No time found: {gpx_file}")
                continue

            year = f"{dt.year:04d}"
            month = f"{dt.month}"
            day = f"{dt.day}"

            target_dir = TARGET_ROOT / year / month / day
            target_dir.mkdir(parents=True, exist_ok=True)

            target_file = target_dir / gpx_file.name

            print(f"→ {gpx_file} → {target_file}")
            shutil.copy2(gpx_file, target_file)


        except Exception as e:
            print(f"❌ Failed: {gpx_file} ({e})")

if __name__ == "__main__":
    organize_gpx_files(Path("C:/MyData/MyOzi"))
