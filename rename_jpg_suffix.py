from pathlib import Path
import uuid

ROOT = Path(r"G:\Ron\MyGPSFotos\rest")  # CHANGE THIS

def rename_jpg_files(root: Path):
    for path in root.rglob("*"):
        if path.is_file() and path.suffix == ".JPG":
            tmp_path = path.with_name(
                path.stem + f".__tmp__{uuid.uuid4().hex}"
            )
            final_path = path.with_suffix(".jpg")

            print(f"RENAME: {path.name} → {final_path.name}")

            # step 1: rename to temp
            path.rename(tmp_path)

            # step 2: rename temp to final
            tmp_path.rename(final_path)

if __name__ == "__main__":
    rename_jpg_files(ROOT)
