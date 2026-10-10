"""Publish a new Cherry: test, version, commit, tag, push, release.

    python3 system/tools/publish.py "Clips can start from any shot's image"
    python3 system/tools/publish.py -F notes.md
    python3 system/tools/publish.py 2.0 "the big one"        # only when you say so

Versions go v1.2, v1.3, v1.4 ... - with no number given, the next one is
picked for you. Give a number only to jump (e.g. to 2.0).

Everyone running Cherry sees "Cherry 1.2.0 is ready" on the home screen and
updates with one click. Needs git and the GitHub CLI (gh), logged in.
"""

import os
import subprocess
import sys

SYSTEM = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ROOT = os.path.dirname(SYSTEM)
ZIP = os.path.join(os.path.dirname(ROOT), "Cherry.zip")


def run(*cmd, check=True, capture=False):
    print("  $", " ".join(cmd))
    r = subprocess.run(cmd, cwd=ROOT, text=True, capture_output=capture)
    if check and r.returncode != 0:
        if capture:
            print(r.stdout, r.stderr)
        sys.exit("stopped: " + cmd[0] + " failed")
    return r


def next_version():
    """1.1.2 -> 1.2, 1.2 -> 1.3: the middle number goes up, nothing else."""
    try:
        cur = open(os.path.join(SYSTEM, "VERSION"), encoding="utf-8").read().strip()
    except OSError:
        cur = "1.0"
    parts = [int(p) for p in cur.split(".") if p.isdigit()] + [0, 0]
    return str(parts[0]) + "." + str(parts[1] + 1)


def main():
    args = sys.argv[1:]
    if args and args[0] in ("-h", "--help"):
        sys.exit(__doc__)
    if args and args[0].replace(".", "").isdigit():
        version = args.pop(0)
    else:
        version = next_version()
    if "-F" in args:
        notes = open(args[args.index("-F") + 1], encoding="utf-8").read()
    else:
        notes = " ".join(args) or "Cherry " + version
    if not all(p.isdigit() for p in version.split(".")) or not 1 <= version.count(".") <= 2:
        sys.exit("version must look like 1.3 (or 2.0)")
    tag = "v" + version
    if run("git", "tag", "-l", tag, capture=True).stdout.strip():
        sys.exit(tag + " already exists - pick the next number")

    print("1/5 self-test")
    r = run(sys.executable, os.path.join(SYSTEM, "app", "selftest.py"), check=False, capture=True)
    tail = (r.stdout or "").strip().splitlines()[-2:]
    print("    " + " | ".join(tail))
    if r.returncode != 0:
        sys.exit("stopped: the self-test failed - nothing was published")

    print("2/5 version " + version)
    with open(os.path.join(SYSTEM, "VERSION"), "w", encoding="utf-8") as fh:
        fh.write(version + "\n")

    print("3/5 commit + tag")
    run("git", "add", "-A")
    if run("git", "diff", "--cached", "--quiet", check=False).returncode != 0:
        run("git", "commit", "-q", "-m", "Cherry " + version + "\n\n" + notes)
    run("git", "tag", "-a", tag, "-m", "Cherry " + version)

    print("4/5 build Cherry.zip")
    run(sys.executable, os.path.join(SYSTEM, "tools", "make_release.py"), ZIP)

    print("5/5 push + release")
    run("git", "push", "-q", "origin", "HEAD", "--follow-tags")
    run("gh", "release", "create", tag, ZIP, "--title", "Cherry " + version, "--notes", notes)
    print("\n  Published Cherry " + version + ". The update button will offer it within an hour"
          " (or straight away after a restart).")


if __name__ == "__main__":
    main()
