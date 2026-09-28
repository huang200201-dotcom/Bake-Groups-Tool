# Changelog

## 1.0.1 — 2026-09-29

- Added public GitHub Release updates without accounts, activation, tokens, signatures or encrypted payloads.
- Enabled automatic updates by default, with an opt-out checkbox and a manual check/download/apply action.
- Added idle-time Python reloads; changed native modules already loaded in Maya are staged until Maya restarts and the plugin is opened again.
- Added release checksums, safe archive extraction, update staging and transaction rollback; source checkouts are excluded from automatic replacement.
- Changed the website action to open the project repository homepage.
- Kept complete drag-and-drop installers. Version 1.0.0 users must install 1.0.1 manually once because 1.0.0 has no updater.

## 1.0.0 — 2026-09-29

First open-source Bake Master release.

- Removed activation, encrypted credential storage, machine binding, revocation and runtime authorization checks from Python and native code.
- Rebuilt native geometry modules for Maya 2022–2027 without private build inputs.
- Reduced the interface to Auto Grouping and Asset Tasks; removed the Manual Correction page.
- Preserved optimized native matching, Cage, island ID colors, chapter export and source-scene protection.
- Retained fixes for FBX filenames, final group membership and low-poly visibility after export.
- Replaced version-stacked source and the commercial update channel with one source tree, a transactional installer, build scripts and versioned GitHub Releases.
- Published Python/C++ source under GPL v3 with upstream and third-party attribution.
