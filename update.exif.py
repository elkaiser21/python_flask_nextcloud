import subprocess
from pathlib import Path

IMAGE_EXTS = {".jpg", ".jpeg", ".tif", ".tiff"}

def has_datetime(image_path):
    cmd = [
        "exiftool",
        "-DateTimeOriginal",
        "-s",
        "-s",
        "-s",
        str(image_path)
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    return bool(result.stdout.strip())

def copy_datetime_fields(source, target):
    cmd = [
        "exiftool",
        "-TagsFromFile", str(source),

        # EXIF
        "-DateTimeOriginal",
        "-CreateDate",
        "-ModifyDate",

        # IPTC
        "-IPTC:DateCreated",
        "-IPTC:TimeCreated",

        "-overwrite_original",
        str(target)
    ]
    subprocess.run(cmd, check=True)

directory = Path("C:\MyData\MyFotoTesting\originals\photo")
#directory = Path("C:/MyWebOkeCloud/MyGPSFotosSync/2025/11/30")

images = [p for p in directory.iterdir() if p.suffix.lower() in IMAGE_EXTS]

# pick a donor image that has DateTimeOriginal
donor = next((img for img in images if has_datetime(img)), None)

if donor is None:
    raise RuntimeError("No image with DateTimeOriginal found")

print(f"Using donor: {donor.name}")

for img in images:
    if not has_datetime(img):
        print(f"Copying date/time → {img.name}")
        copy_datetime_fields(donor, img)
