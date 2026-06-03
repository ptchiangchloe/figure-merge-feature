# Merge feature — notes

## Approach

**Backend (`POST /merge`)**
- Accepts `{ "keys": [...], "panels": {...}, "orientation", "gap", "align" }`
  (two or more keys). The client sends the **panel data** (`path`, `page`,
  `bbox_normalized`, `callout_numbers`, …) for the selected images, so the
  endpoint is **fully stateless** — it reads and writes no server-side state.
- Panels are sorted into reading order (`page → top → left`); the top-left-most
  panel becomes the **representative** and keeps its key. The others are removed
  from the state and the image list (by the client).
- **Stitching strategy** (two tiers):
  0. **Explicit user choice (horizontal / vertical).** The merge UI lets the user
     pick how the panels are combined; the crops are stitched side-by-side or
     stacked at **native size (no stretching)**, with a customizable white `gap`
     between panels and an outer border, and aligned (left/center/right, which
     maps to top/middle/bottom for horizontal layout).
  1. **Same page (auto) → re-crop the union bbox from the PDF.** Each entry already
     carries `bbox_normalized` + `page` + `dpi`. I take the smallest box enclosing
     all panels and re-rasterize that region straight from `article.pdf` at the
     original DPI. This reproduces the panels' *true* spatial layout
     (horizontal / vertical / diagonal) and is exactly what the clustering step
     does when it groups fragments — so a manual merge is consistent with an
     automatic one. This is the path Figure 3 (panels A + B on page 6) takes.
  2. **Cross-page or missing bbox → stitch the existing PNGs.** Crops are
     concatenated at native size (no scaling); orientation is inferred from the
     bbox spread (wider spread → side-by-side, else stacked), defaulting to
     vertical.
- The server returns the merged figure **inline as a base64 data URL**, together
  with the union `bbox_normalized`, the combined `callout_numbers`, and a
  `merged_from` provenance list. Nothing is written to disk.

**Frontend (`image-view-tester.html`)**
- A **Merge** button opens a modal with a selectable thumbnail grid of every
  image. The image you were viewing is **pre-selected but deselectable**, so you
  can combine any pair without folding in the current (possibly already-merged)
  figure. Confirm with a button that reads "Merge N images" (minimum two).
- Intentional friction so it's **hard to do by accident**: a deliberate modal +
  explicit multi-select + a confirm button (not a drag, not a single click on
  the image itself).
- Controls for **layout** (horizontal / vertical), **padding**, and **alignment**
  before confirming.
- On success the merged image replaces the representative, the absorbed entries
  disappear from the list, and the view jumps to the merged figure. The client
  applies the change **surgically** to its own in-memory state, so repeated
  merges stack correctly.
- A **Reset** button (`POST /reset`) discards all merges/edits and restores the
  original extracted images.
- A **full-view lightbox** opens any figure full-bleed without stretching it.

## Data integrity
- The **client owns the state** (single source of truth). Because the merged
  image rides along as an inline data URL, there is no shared mutable server
  state to drift between requests — what you see always matches what's stored.
- The **representative's** editable metadata (figure number, caption) is kept;
  callout labels from all panels are merged so labels from B carry over to the
  combined figure.
- State stays consistent: exactly one entry replaces N, no orphaned keys, image
  list and state map updated together.

## Tradeoffs
- **Union re-crop vs. tight stitch.** The union crop includes whatever sits
  *between* panels (usually whitespace / panel labels). For genuine sub-panels
  that's correct and matches the pipeline; for panels that are far apart it can
  pull in unrelated content. The PNG-stitch path avoids that but loses true
  spacing — I use it only when re-cropping isn't possible.
- **Stateless over durable, for now.** Making merges client-owned + inline fixed
  the serverless drift (on Vercel, `/tmp` is per-instance and doesn't survive
  between requests), and makes local and serverless behave identically. The cost
  is that state lives only in the session — a hard reload starts fresh.
- **Inline data URLs grow.** Re-merging an already-merged figure ships its pixels
  back to the server in the request body — fine at this scale, but a blob store
  would be leaner.

## What I'd improve with more time
- **Durable storage** (e.g. Vercel Blob / S3 for images + a KV/DB for state) so
  merges survive reloads and cold starts.
- An **undo / unmerge** action (the `merged_from` field already records sources).
- Smarter layout reconstruction for the stitch fallback (align panels by their
  page coordinates instead of a simple concat), and a **preview** of the merged
  result inside the modal before confirming.
- Let the user pick which panel's metadata wins, instead of always the
  top-left one.
- **Export as ZIP** — bundle the original files plus the merged result, with a
  CSV manifest mapping each output to its source panels.
- **Better error handling & user feedback** — graceful states when an image
  fails to load, plus clear inline messages and retries instead of silent
  failures.
- Improve the **mobile experience**: the review UI and merge modal are laid out
  for desktop; on small screens the image card + fields panel should stack
  responsively, the merge thumbnail grid and controls need touch-friendly
  sizing, and the full-view lightbox should support pinch-to-zoom.
