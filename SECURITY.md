# Security and data safety

PhotoAI is a local desktop application, not a network service. Do not expose its
development server to the internet. Treat the session token and data directory
as private. Download executables only from this repository's Releases.

The XMP cleanup tool permanently deletes the files in the confirmed scan.
Keep backups and test new versions using copies, especially before XMP writes,
Lightroom processing, file cleanup, or RAW/JPEG synchronization.

For a security issue, use GitHub's private vulnerability reporting when enabled.
Do not publish access tokens, full local task logs, catalog files or personal
photos in a public issue. For ordinary bugs, use Issues with redacted steps and
error text. Beta releases are provided without warranty under the MIT license.
