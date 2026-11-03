# Tauri NSIS template provenance

Source: https://raw.githubusercontent.com/tauri-apps/tauri/tauri-cli-v2.11.4/crates/tauri-bundler/src/bundle/windows/nsis/installer.nsi

Pinned tag: `tauri-cli-v2.11.4`, matching the project's npm CLI dependency.
Unmodified source SHA-256:
`20f4ecc730defb71f1342eaeaec4021df13be3d843abba0effe88ea5835fa079`.

The template is licensed MIT OR Apache-2.0 by the Tauri Apps Contributors.
The MIT license text from the same tag is included here.
This integration uses the MIT option.

Local changes are limited to provenance comments and two definitions:
`UNINSTKEY` and `MANUPRODUCTKEY` keep the original `照片选片` suffix. The visible
product name, executable identity, resources, update flow and existing safety
hooks remain supplied by Tauri configuration and its standard template.
This lets a renamed product discover the original installation and update its
existing uninstall entry instead of creating a second registration.

Review this small patch when updating the pinned Tauri CLI/template version.
