"""Publish a new Cherry: test, version, commit, tag, push, release.

    python3 system/tools/publish.py 1.2.0 "Clips can start from any shot's image"
    python3 system/tools/publish.py 1.2.0 -F notes.md

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


def main():
    args = sys.argv[1:]
    if not args or args[0].startswith("-"):
        sys.exit(__doc__)
    version = args[0].lstrip("vV")
    if "-F" in args:
        notes = open(args[args.index("-F") + 1], encoding="utf-8").read()
    else:
        notes = " ".join(a for a in args[1:]) or "Cherry " + version
    if not all(p.isdigit() for p in version.split(".")) or version.count(".") != 2:
        sys.exit("version must look like 1.2.0")
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
