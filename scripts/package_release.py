"""Build a verified source archive from the explicit release manifest."""

import argparse
import gzip
import hashlib
import io
from pathlib import Path, PurePosixPath
import tarfile


ROOT = Path(__file__).resolve().parents[1]


def source_files(root):
    manifest = root / "MANIFEST.sha256"
    files = {}
    for line in manifest.read_text(encoding="utf-8").splitlines():
        digest, relative = line.split(maxsplit=1)
        relative = relative.removeprefix("*").removeprefix("./")
        path = PurePosixPath(relative)
        if (path.is_absolute() or ".." in path.parts
                or any(part.startswith(".") and part != ".gitignore"
                       for part in path.parts)):
            raise ValueError(f"Invalid manifest path: {relative}")
        source = root.joinpath(*path.parts)
        if any(parent.is_symlink() for parent in [source, *source.parents]
               if parent != root and root in parent.parents):
            raise ValueError(f"Symlink is not a release source: {relative}")
        data = source.read_bytes()
        if hashlib.sha256(data).hexdigest() != digest:
            raise ValueError(f"Release checksum mismatch: {relative}")
        if relative in files:
            raise ValueError(f"Duplicate manifest path: {relative}")
        files[relative] = data
    files["MANIFEST.sha256"] = manifest.read_bytes()
    return files


def package(root, output):
    files = source_files(root)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("wb") as raw, gzip.GzipFile(
            filename="", mode="wb", fileobj=raw, mtime=0) as compressed:
        with tarfile.open(fileobj=compressed, mode="w") as archive:
            for relative, data in sorted(files.items()):
                entry = tarfile.TarInfo(f"ANIS/{relative}")
                entry.size = len(data)
                entry.mode = 0o755 if relative.startswith("bin/") else 0o644
                entry.uid = entry.gid = entry.mtime = 0
                entry.uname = entry.gname = ""
                archive.addfile(entry, io.BytesIO(data))
    return len(files)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    version = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
    output = args.output or ROOT / "dist" / f"ANIS-{version}.tar.gz"
    count = package(ROOT, output)
    print(f"Packaged {count} verified files: {output.name}")


if __name__ == "__main__":
    main()
