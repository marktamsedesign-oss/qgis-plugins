# MarkT's QGIS plugins

| Plugin | Folder | What it does |
|---|---|---|
| Route Drawer | `route_drawer/` | Draw routes along roads, import CSV/KMZ, export KMZ/Excel, pins in boundary |
| Quick Find | `quick_find/` | Toolbar search, lat/long pins, saved pins, layer filter |

Works on QGIS 3.22+ and QGIS 4, Windows and Mac.

## Install and get automatic updates in QGIS

1. **Plugins ▸ Manage and Install Plugins… ▸ Settings**
2. Tick **Check for updates on startup** and pick **every time QGIS starts**.
3. Click **Add…** and enter:
   - Name: `MarkT plugins`
   - URL: `https://raw.githubusercontent.com/marktamsedesign-oss/qgis-plugins/main/plugins.xml`
4. Click **OK**, then **Reload all repositories**.
5. Under **All**, find Route Drawer and Quick Find. If you already installed them from a zip, they show as installed, and an **Upgrade** button appears whenever a newer version is published.

## Publishing an update (maintainer)

1. Change the code in the plugin's folder.
2. Raise `version=` in that folder's `metadata.txt` (e.g. `1.11.0` → `1.11.1`).
3. Commit and push to `main`.

GitHub Actions (`.github/workflows/publish.yml`) then:
- zips the folder as `<folder>.<version>.zip`
- creates a Release tagged `<folder>-v<version>`
- regenerates `plugins.xml`

Allow about a minute for the workflow and up to 5 minutes for GitHub's raw-file cache. A push that doesn't raise a version changes nothing for users.

Optional: add a `changelog=` line to `metadata.txt`. It is used for the release notes and shown in Plugin Manager.

### Rules (from how QGIS reads `plugins.xml`)
- QGIS takes the plugin ID from the zip name up to the **first dot**, so zips are named `route_drawer.1.11.0.zip`, never `route_drawer_1.11.0.zip`. Plugin folder names must not contain dots or dashes.
- Keep `qgisMaximumVersion=4.99` in `metadata.txt`. Without it, QGIS assumes 3.99 and hides the plugin on QGIS 4.
- Check locally before pushing: `python3 tools/build_repo.py check`
