# Changelog

## 1.0.0 — 2026-11-02

- Add `download add <url>` and `download add playlist` for the normal
  download, sorting, and Apple Music import workflow.
- Add verified raw intake, persistent SQLite state, provider caching, and
  conservative genre placement with `Review` as the fallback.
- Add immutable publish plans, checksum-verified library copies, and safe
  resume handling for interrupted Music imports.
- Add Apple Music import through its supported AppleScript interface with
  persistent-ID and source-location recording.
- Move active pipeline state and yt-dlp ID archives into
  `~/Music/Library/.pipeline/`.
