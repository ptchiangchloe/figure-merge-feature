# Merge feature — notes

## Approach

**Backend (`POST /merge`)**
- Accepts `{ "keys": ["EXTRACTED_IMAGE_5", "EXTRACTED_IMAGE_6", ...] }` (two or more).
- Panels are sorted into reading order (`page → top → left`); the top-left-most
  panel becomes the **representative** and keeps its key. The others are removed
  from the state and the image list.
- **Stitching strategy** (two tiers):
  1. **Same page → re-crop the union bbox from the PDF.** Each entry already
     carries `bbox_normalized` + `page` + `dpi`. I take the smallest box enclosing
     all panels and re-rasterize that region straight from `article.pdf` at the
     original DPI. This reproduces the panels' *true* spatial layout
     (horizontal / vertical / diagonal) and is exactly what the clustering step
     does when it groups fragments — so a manual merge is consistent with an
     automatic one. This is the path Figure 3 (panels A + B on page 6) takes.
  2. **Cross-page or missing bbox → stitch the existing PNGs.** Panels are scaled
     to a shared edge and concatenated; orientation is inferred from the bbox
     spread (wider spread → side-by-side, else stacked), defaulting to vertical.
- The merged PNG is written to the images dir and the state is rewritten with the
  representative updated in place (new `path`, union `bbox_normalized`, combined
  `callout_numbers`, and a `merged_from` provenance list).

**Frontend (`image-view-tester.html`)**
- A **Merge** button opens a modal showing the current image as the anchor plus a
  selectable thumbnail grid of every other image. The user ticks one or more and
  confirms with a button that reads "Merge N images".
- Intentional friction so it's **hard to do by accident**: it's a deliberate
  modal + explicit multi-select + a confirm button (not a drag, not a single
  click on the image itself).
- On success the merged image replaces the current one, the absorbed entries
  disappear from the list, and the view jumps to the merged figure.

## Data integrity
- The **representative's** editable metadata (figure number, caption) is kept;
  callout labels from all panels are merged so labels from B carry over to the
  combined figure.
- State stays consistent: exactly one entry replaces N, no orphaned keys, image
  list and state map updated together.
- Writes are environment-aware: locally they go next to the project; on Vercel's
  read-only filesystem they go to `/tmp`, and `/state` serves the committed
  snapshot.

## Tradeoffs
- **Union re-crop vs. tight stitch.** The union crop includes whatever sits
  *between* panels (usually whitespace / panel labels). For genuine sub-panels
  that's correct and matches the pipeline; for panels that are far apart it can
  pull in unrelated content. The PNG-stitch path avoids that but loses true
  spacing — I use it only when re-cropping isn't possible.
- **Persistence is per-instance.** On serverless, merged output lives in `/tmp`,
  which is ephemeral (lost on cold start, not shared across instances). Fine for
  a demo; not durable.
- Locally `/state` re-runs extraction on every call (the given behavior), so a
  merge is reflected live in the session but reset on a hard reload.

## What I'd improve with more time
- Durable storage (e.g. Vercel Blob / S3 for images + a KV/DB for state) so
  merges survive reloads and cold starts.
- An **undo / unmerge** action (the `merged_from` field already records sources).
- Smarter layout reconstruction for the stitch fallback (align panels by their
  page coordinates instead of a simple concat), and a preview of the merged
  result inside the modal before confirming.
- Let the user pick which panel's metadata wins, instead of always the
  top-left one.
