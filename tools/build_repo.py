#!/usr/bin/env python3
"""Build tool for a self-hosted QGIS plugin repository on GitHub.

Every top-level folder that contains a metadata.txt is treated as one plugin.
Standard library only, so it runs on any GitHub runner (or your own computer).

Commands
    check                         validate every plugin's metadata.txt
    list                          print "<folder> <version>" per plugin
    zip  <folder> <out_dir>       build <out_dir>/<folder>.<version>.zip
    notes <folder>                print release notes (changelog or a default line)
    xml  --repo OWNER/NAME [--branch main] [--out plugins.xml]
                                  write the plugins.xml that QGIS reads

Rules that matter to QGIS (checked against QGIS's pyplugin_installer source):
  * QGIS takes the plugin ID from <file_name> up to the FIRST dot, so the zip
    must be named "<folder>.<version>.zip" and the folder name must not contain
    a dot. "route_drawer_1.11.0.zip" would be read as a plugin called
    "route_drawer_1" and would never update your installed "route_drawer".
  * The version QGIS compares is the "version" attribute of <pyqgis_plugin>.
  * If <qgis_maximum_version> is missing QGIS assumes "<min major>.99"
    (e.g. 3.99), which hides the plugin on QGIS 4, so it is always written.
  * Release tag per plugin version: "<folder>-v<version>".
"""

import argparse
import configparser
import datetime as dt
import os
import re
import subprocess
import sys
import zipfile
from pathlib import Path
from xml.sax.saxutils import escape, quoteattr

ROOT = Path(__file__).resolve().parent.parent

# Keys QGIS needs (custom repositories don't enforce the official repo's extra keys).
REQUIRED = ["name", "qgisMinimumVersion", "description", "version", "author"]

EXCLUDE_DIRS = {"__pycache__", ".git", ".idea", ".vscode", "__MACOSX", ".pytest_cache"}
EXCLUDE_FILES = {".DS_Store", "Thumbs.db", "desktop.ini"}
EXCLUDE_SUFFIXES = (".pyc", ".pyo", ".orig", ".rej", ".swp")

FOLDER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
VERSION_RE = re.compile(r"^\d+(\.\d+){0,3}([.-]?[A-Za-z0-9]+)?$")


# --------------------------------------------------------------------------- helpers
def plugin_folders():
    return sorted(p for p in ROOT.iterdir()
                  if p.is_dir() and not p.name.startswith(".") and (p / "metadata.txt").is_file())


def read_metadata(folder: Path) -> dict:
    cp = configparser.ConfigParser(interpolation=None, strict=False)
    cp.optionxform = str  # keep camelCase keys (qgisMinimumVersion)
    with open(folder / "metadata.txt", encoding="utf-8-sig") as fh:
        cp.read_file(fh)
    if not cp.has_section("general"):
        raise SystemExit(f"{folder.name}/metadata.txt has no [general] section")
    return {k: v.strip() for k, v in cp.items("general")}


def git_date(args):
    """ISO date (UTC, no zone) from git log, or None outside a git checkout."""
    try:
        out = subprocess.run(["git", "log", "--format=%ct", *args], cwd=ROOT,
                             capture_output=True, text=True, check=True).stdout.split()
    except (OSError, subprocess.CalledProcessError):
        return None
    if not out:
        return None
    ts = int(out[0])
    return dt.datetime.fromtimestamp(ts, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def zip_name(folder: str, version: str) -> str:
    return f"{folder}.{version}.zip"


def tag_name(folder: str, version: str) -> str:
    return f"{folder}-v{version}"


def is_bool_true(value: str) -> bool:
    return value.strip().lower() in ("true", "yes", "1")


# --------------------------------------------------------------------------- commands
def cmd_check(_args):
    problems, warnings = [], []
    folders = plugin_folders()
    if not folders:
        problems.append("no plugin folders (folders with metadata.txt) found")
    for folder in folders:
        md = read_metadata(folder)
        n = folder.name
        if not FOLDER_RE.match(n):
            problems.append(f"{n}: folder name must be a Python module name "
                            "(letters, digits, underscore; no dots or dashes)")
        for key in REQUIRED:
            if not md.get(key):
                problems.append(f"{n}: metadata.txt is missing '{key}'")
        ver = md.get("version", "")
        if ver and not VERSION_RE.match(ver):
            problems.append(f"{n}: version '{ver}' doesn't look like 1.2.3")
        if not md.get("qgisMaximumVersion"):
            warnings.append(f"{n}: no qgisMaximumVersion; plugins.xml will use "
                            f"{md.get('qgisMinimumVersion', '3')[0]}.99 (hides it on the next major QGIS)")
        if not (folder / "__init__.py").is_file():
            problems.append(f"{n}: __init__.py missing")
        icon = md.get("icon")
        if icon and not (folder / icon).is_file():
            warnings.append(f"{n}: icon '{icon}' not found in the folder")
    for w in warnings:
        print("WARNING:", w)
    for p in problems:
        print("ERROR:", p)
    if problems:
        sys.exit(1)
    print(f"OK: {len(folders)} plugin(s) checked")


def cmd_list(_args):
    for folder in plugin_folders():
        print(folder.name, read_metadata(folder)["version"])


def cmd_zip(args):
    folder = ROOT / args.folder
    md = read_metadata(folder)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / zip_name(folder.name, md["version"])
    files = []
    for dirpath, dirnames, filenames in os.walk(folder):
        dirnames[:] = sorted(d for d in dirnames if d not in EXCLUDE_DIRS)
        for f in sorted(filenames):
            if f in EXCLUDE_FILES or f.endswith(EXCLUDE_SUFFIXES):
                continue
            files.append(Path(dirpath) / f)
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in files:
            # top-level folder inside the zip = plugin folder name (what QGIS installs)
            zf.write(path, path.relative_to(ROOT).as_posix())
    print(target)


def cmd_notes(args):
    md = read_metadata(ROOT / args.folder)
    changelog = md.get("changelog", "").strip()
    if changelog:
        print(changelog)
    else:
        print(f"{md['name']} {md['version']}: install or update from QGIS "
              "(Plugins > Manage and Install Plugins) using this repository's plugins.xml.")


def cmd_xml(args):
    repo = args.repo
    branch = args.branch
    raw_base = f"https://raw.githubusercontent.com/{repo}/{branch}"
    home = f"https://github.com/{repo}"
    entries = []
    now = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    for folder in plugin_folders():
        md = read_metadata(folder)
        n, ver = folder.name, md["version"]
        qmin = md.get("qgisMinimumVersion", "3.0")
        qmax = md.get("qgisMaximumVersion") or f"{qmin[0]}.99"
        fname = zip_name(n, ver)
        download = f"{home}/releases/download/{tag_name(n, ver)}/{fname}"
        icon = md.get("icon", "")
        icon_url = f"{raw_base}/{n}/{icon}" if icon else ""
        created = git_date(["--reverse", "--", n]) or now
        updated = git_date(["-1", "--", f"{n}/metadata.txt"]) or now

        def el(tag, value, cdata=False):
            value = "" if value is None else str(value)
            if cdata:
                value = value.replace("]]>", "]]]]><![CDATA[>")
                return f"    <{tag}><![CDATA[{value}]]></{tag}>"
            return f"    <{tag}>{escape(value)}</{tag}>"

        lines = [
            f"  <pyqgis_plugin name={quoteattr(md['name'])} version={quoteattr(ver)}>",
            el("description", md.get("description", ""), cdata=True),
            el("about", md.get("about", ""), cdata=True),
            el("version", ver),
            el("trusted", "False"),
            el("qgis_minimum_version", qmin),
            el("qgis_maximum_version", qmax),
            el("homepage", md.get("homepage") or home),
            el("file_name", fname),
            el("icon", icon_url),
            el("author_name", md.get("author", ""), cdata=True),
            el("author_email", md.get("email", "")),
            el("download_url", download),
            el("uploaded_by", md.get("author", ""), cdata=True),
            el("create_date", created),
            el("update_date", updated),
            el("experimental", "True" if is_bool_true(md.get("experimental", "False")) else "False"),
            el("deprecated", "True" if is_bool_true(md.get("deprecated", "False")) else "False"),
            el("tracker", md.get("tracker") or f"{home}/issues"),
            el("repository", md.get("repository") or home),
            el("tags", md.get("tags", ""), cdata=True),
            el("category", md.get("category", "")),
            el("changelog", md.get("changelog", ""), cdata=True),
            el("downloads", "0"),
            el("average_vote", "0"),
            el("rating_votes", "0"),
            el("external_dependencies", ""),
            el("plugin_dependencies", md.get("plugin_dependencies", "")),
            el("server", "True" if is_bool_true(md.get("server", "False")) else "False"),
            "  </pyqgis_plugin>",
        ]
        entries.append("\n".join(lines))
    xml = ('<?xml version="1.0" encoding="UTF-8"?>\n'
           "<!-- Generated by tools/build_repo.py - do not edit by hand. -->\n"
           "<plugins>\n" + "\n".join(entries) + "\n</plugins>\n")
    Path(args.out).write_text(xml, encoding="utf-8")
    print(f"wrote {args.out} ({len(entries)} plugin(s))")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check").set_defaults(func=cmd_check)
    sub.add_parser("list").set_defaults(func=cmd_list)
    z = sub.add_parser("zip")
    z.add_argument("folder")
    z.add_argument("out_dir")
    z.set_defaults(func=cmd_zip)
    nt = sub.add_parser("notes")
    nt.add_argument("folder")
    nt.set_defaults(func=cmd_notes)
    x = sub.add_parser("xml")
    x.add_argument("--repo", required=True, help="OWNER/NAME, e.g. marktamsedesign-oss/qgis-plugins")
    x.add_argument("--branch", default="main")
    x.add_argument("--out", default=str(ROOT / "plugins.xml"))
    x.set_defaults(func=cmd_xml)
    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
