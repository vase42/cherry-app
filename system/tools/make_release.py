"""Build Cherry.zip for sending to someone else.

    python system/tools/make_release.py            -> Cherry.zip beside the Cherry folder
    python system/tools/make_release.py OUT.zip

The zip holds the program and nothing personal: no database (it has the API
keys), no Output, no references or characters. Everything sits inside one
top-level "Cherry" folder, so unzipping gives a single folder, and unzipping a
newer one over it replaces the program without touching anyone's work.
"""

import os
import sys
import zipfile

SYSTEM = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ROOT = os.path.dirname(SYSTEM)

TOP_FILES = ["Start Cherry.bat", "Make desktop shortcut.bat", "READ ME.txt"]
# Personal or generated: never shipped.
SKIP_DIRS = {"data", "refs", "characters", "__pycache__", "Output", "out", ".git", "research"}
SKIP_FILES = {".DS_Store", "Thumbs.db", "desktop.ini", "Cherry.lnk", ".gitkeep"}
SKIP_EXT = {".db", ".db-wal", ".db-shm", ".pyc", ".lnk"}


def main():
    out = os.path.abspath(sys.argv[1] if len(sys.argv) > 1
                          else os.path.join(os.path.dirname(ROOT), "Cherry.zip"))
    names = []
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for f in TOP_FILES:
            z.write(os.path.join(ROOT, f), "Cherry/" + f)
            names.append(f)
        z.writestr("Cherry/Output/", "")          # the folder exists from the start
        for dirpath, dirnames, filenames in os.walk(SYSTEM):
            rel_dir = os.path.relpath(dirpath, SYSTEM)
            if rel_dir == ".":
                dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
            else:
                dirnames[:] = [d for d in dirnames if d not in ("__pycache__", ".git", "research")]
            for f in sorted(filenames):
                if f in SKIP_FILES or os.path.splitext(f)[1] in SKIP_EXT or f.endswith(("-wal", "-shm")):
                    continue
                full = os.path.join(dirpath, f)
                arc = "Cherry/system/" + os.path.relpath(full, SYSTEM).replace(os.sep, "/")
                z.write(full, arc)
                names.append(arc)

    # Belt and braces: refuse to leave behind a zip with anything personal in it.
    with zipfile.ZipFile(out) as z:
        bad = [n for n in z.namelist()
               if n.endswith((".db", "-wal", "-shm")) or "/data/" in n
               or "/refs/" in n or "/characters/" in n or n.startswith("Cherry/Output/") and n != "Cherry/Output/"]
    if bad:
        os.remove(out)
        sys.exit("refusing to build: personal files would be included: " + ", ".join(bad[:5]))
    print("wrote", out, "-", len(names), "files,", round(os.path.getsize(out) / 1024), "KB")


if __name__ == "__main__":
    main()
