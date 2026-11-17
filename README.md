# MarkT's QGIS plugins

| Plugin | Folder | What it does |
|---|---|---|
| Route Drawer | `route_drawer/` | Draw routes along roads, import CSV/KMZ, export KMZ/Excel, pins in boundary |
| Quick Find | `quick_find/` | Toolbar search, lat/long pins, saved pins, layer filter |

Works on QGIS 3.22+ and QGIS 4, Windows and Mac.

## Install and get automatic updates in QGIS

**Easiest:** download the plugin's zip from [Releases](https://github.com/marktamsedesign-oss/qgis-plugins/releases) (e.g. `route_drawer.1.12.0.zip`), then in QGIS go to **Plugins ▸ Manage and Install Plugins… ▸ Install from ZIP**. That's all:

- The plugin adds the **MarkT plugins** repository to the Plugin Manager by itself.
- Each time QGIS starts, it checks for a newer version. If there is one, the message bar shows it with an **Update now** button, which installs it with QGIS's own installer and restarts the plugin. Your layers and project are not touched.
- **Check for updates…** in the plugin's menu (Plugins ▸ Route Drawer / Quick Find) checks right away.
- To stop the update messages, untick **MarkT plugins** in **Plugins ▸ Manage and Install Plugins… ▸ Settings**. **Check for updates…** turns it back on.

This works from Route Drawer 1.12.0 and Quick Find 1.13.0 on. If you have an older version, install the new zip once; later versions then arrive on their own.

**Or add the repository by hand** (also lists the plugins under **All** for a first install):

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

### The in-plugin updater (`updater.py`)

Every plugin folder has the same `updater.py`, started from the plugin's `initGui()` and stopped in `unload()`. It:
- adds the repository to the Plugin Manager settings the first time the plugin loads. A repository the user unticked or deleted stays that way, unless they pick **Check for updates…**;
- 5 seconds after QGIS starts, downloads `plugins.xml` (nothing is sent) and offers **Update now** when it lists a newer version that runs on that QGIS;
- installs through QGIS's own installer (`pyplugin_installer`), the same code as **Upgrade** in the Plugin Manager.

A copy that QGIS did not install (a link to this checkout, or one loaded through `QGIS_PLUGINPATH`) is never replaced and gets no automatic messages, so your development copy is safe.

To change it, edit one copy and copy it to the other plugin folders. `tools/build_repo.py check` fails if the copies differ. In GitHub Actions it also fails if `REPO_URL` isn't this repository's `plugins.xml`, e.g. in a fork. A new plugin folder should get a copy too, wired like `route_drawer/plugin.py` does (`Updater(self.iface, MENU)`).

### Rules (from how QGIS reads `plugins.xml`)
- QGIS takes the plugin ID from the zip name up to the **first dot**, so zips are named `route_drawer.1.11.0.zip`, never `route_drawer_1.11.0.zip`. Plugin folder names must not contain dots or dashes.
- Keep `qgisMaximumVersion=4.99` in `metadata.txt`. Without it, QGIS assumes 3.99 and hides the plugin on QGIS 4.
- Check locally before pushing: `python3 tools/build_repo.py check`
