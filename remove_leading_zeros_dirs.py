import os
from pathlib import Path

ROOT = Path(r"C:/MyWebOkeCloud/MyTracksSync")  # CHANGE THIS

def normalize_dir_name(name: str) -> str:
    if name.isdigit():
        new = name.lstrip("0")
        return new if new else "0"
    return name

def rename_dirs(root: Path):
    for current_path, dirnames, _ in os.walk(root, topdown=False):
        current_path = Path(current_path)
        year = current_path.parts[-1]
        for dirname in dirnames:
            old_dir = current_path / dirname
            new_name = normalize_dir_name(dirname)

            if new_name != dirname:
                new_dir = current_path / new_name

                if new_dir.exists():
                    raise RuntimeError(
                        f"Target already exists: {new_dir}"
                    )

                print(f"RENAME: {old_dir} → {new_dir}")
                old_dir.rename(new_dir)

if __name__ == "__main__":
    rename_dirs(ROOT)
