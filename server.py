import os
import json
from io import BytesIO

from flask import Flask, jsonify, send_from_directory, abort, request
from flask_cors import CORS

# Importing the parser pulls in PyMuPDF. On a read-only serverless host we only
# serve the pre-extracted artifacts, so don't let a missing/heavy dependency
# stop the app from booting.
try:
    import document_parser
    extract_figures = document_parser.extract_figures
except Exception:  # pragma: no cover - serverless fallback
    document_parser = None
    extract_figures = None

app = Flask(__name__)
CORS(app)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# Vercel (and Lambda generally) only allow writes to /tmp. Detect that and
# point all writable paths there; everything else reads from the committed
# files bundled with the function.
ON_SERVERLESS = bool(os.environ.get("VERCEL") or os.environ.get("AWS_LAMBDA_FUNCTION_NAME"))
DATA_DIR = "/tmp/figure-merge" if ON_SERVERLESS else BASE_DIR

PDF_PATH = os.path.join(BASE_DIR, "article.pdf")
TEXTRACT_PATH = os.path.join(BASE_DIR, "analyzeDocResponse.json")

# Images committed to the repo (read-only on serverless).
IMAGES_SRC = os.path.join(BASE_DIR, "images")
# Freshly extracted / merged images (writable).
IMAGES_OUT = os.path.join(DATA_DIR, "images")

# state.json committed to the repo vs. the writable copy produced at runtime.
COMMITTED_STATE = os.path.join(BASE_DIR, "state.json")
STATE_OUT = os.path.join(DATA_DIR, "state.json")


# -----------------------------------------------------------------------------
# State helpers
# -----------------------------------------------------------------------------

def _load_state():
    """Return the current working state, preferring the writable copy."""
    for path in (STATE_OUT, COMMITTED_STATE):
        if os.path.exists(path):
            with open(path) as f:
                return json.load(f)
    if extract_figures is not None:
        return extract_figures(
            PDF_PATH, TEXTRACT_PATH, output_dir=IMAGES_OUT, state_json_path=STATE_OUT
        )
    return {}


def _save_state(state):
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(STATE_OUT, "w") as f:
        json.dump(state, f, indent=2)


def _resolve_image_path(filename):
    """Locate an image file across the writable and committed image dirs."""
    for base in (IMAGES_OUT, IMAGES_SRC):
        candidate = os.path.join(base, filename)
        if os.path.exists(candidate):
            return candidate
    return None


def _remove_merged_images():
    """Delete merge outputs we generated, leaving the original crops intact."""
    if not os.path.isdir(IMAGES_OUT):
        return
    for fn in os.listdir(IMAGES_OUT):
        if fn.startswith("merged_"):
            try:
                os.remove(os.path.join(IMAGES_OUT, fn))
            except OSError:
                pass


def _fresh_state():
    """The original extracted state, discarding any merges/edits."""
    # Local dev with the parser available: re-extract from scratch (this also
    # rewrites STATE_OUT + the original crops).
    if not ON_SERVERLESS and extract_figures is not None:
        return extract_figures(
            PDF_PATH, TEXTRACT_PATH, output_dir=IMAGES_OUT, state_json_path=STATE_OUT
        )
    # Otherwise drop the writable copy and fall back to the committed snapshot.
    if STATE_OUT != COMMITTED_STATE and os.path.exists(STATE_OUT):
        try:
            os.remove(STATE_OUT)
        except OSError:
            pass
    with open(COMMITTED_STATE) as f:
        return json.load(f)


# -----------------------------------------------------------------------------
# Merge: image stitching
# -----------------------------------------------------------------------------

# Default white gap (px) inserted between panels when stitching crops together;
# the client can override this per merge.
_STITCH_GAP_PX = 24
_STITCH_GAP_MAX_PX = 500


def _grey_png_buffer(pil_img):
    """Save any PIL image as an 8-bit greyscale PNG buffer (matches the pipeline)."""
    if pil_img.mode != "L":
        pil_img = pil_img.convert("L")
    buf = BytesIO()
    pil_img.save(buf, format="PNG")
    buf.seek(0)
    return buf


def _load_panel_image(path):
    """Open a panel image as greyscale. `path` may be a committed filename or an
    inline `data:image/...;base64,` URL (used for already-merged panels so the
    request is self-contained and doesn't depend on any server-side file)."""
    from PIL import Image
    if isinstance(path, str) and path.startswith("data:"):
        import base64
        b64 = path.split(",", 1)[1]
        return Image.open(BytesIO(base64.b64decode(b64))).convert("L")
    resolved = _resolve_image_path(path)
    if resolved is None:
        raise FileNotFoundError(path)
    return Image.open(resolved).convert("L")


def _pad_image(pil_img, pad):
    """Return the image centred on a white canvas with `pad` px margin on all sides."""
    if pad <= 0:
        return pil_img
    from PIL import Image
    canvas = Image.new("L", (pil_img.width + 2 * pad, pil_img.height + 2 * pad), 255)
    canvas.paste(pil_img.convert("L"), (pad, pad))
    return canvas


def _union_bbox(entries):
    """Smallest normalized bbox enclosing every panel (entries must share a page)."""
    boxes = [e["bbox_normalized"] for _, e in entries]
    left = min(b["Left"] for b in boxes)
    top = min(b["Top"] for b in boxes)
    right = max(b["Left"] + b["Width"] for b in boxes)
    bottom = max(b["Top"] + b["Height"] for b in boxes)
    return {"Left": left, "Top": top, "Width": right - left, "Height": bottom - top}


def _same_page(entries):
    """True if every panel is on the same page and carries a bbox."""
    if not all(e.get("bbox_normalized") and e.get("page") for _, e in entries):
        return False
    return len({e["page"] for _, e in entries}) == 1


def _recrop_union_from_pdf(entries):
    """
    "Auto" merge: when all panels are on the same page, re-rasterize their union
    region straight from the PDF. This reconstructs the panels' true spatial
    layout (the same thing the clustering step does when it groups fragments),
    regardless of horizontal/vertical/diagonal arrangement.

    Returns a greyscale PIL image, or None if it can't be done.
    """
    if document_parser is None or not _same_page(entries):
        return None

    try:
        import fitz
        from PIL import Image
    except Exception:
        return None

    page_num = entries[0][1]["page"]
    union = _union_bbox(entries)
    dpi = entries[0][1].get("dpi", 200)

    doc = fitz.open(PDF_PATH)
    try:
        if page_num < 1 or page_num > len(doc):
            return None
        page = doc[page_num - 1]
        zoom = dpi / 72.0
        pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
        img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
        w, h = img.size
        box = (
            int(union["Left"] * w),
            int(union["Top"] * h),
            int((union["Left"] + union["Width"]) * w),
            int((union["Top"] + union["Height"]) * h),
        )
        crop = img.crop(box)
    finally:
        doc.close()

    return crop.convert("L")


def _decide_orientation(entries):
    """Side-by-side ('h') if panels spread more horizontally than vertically, else 'v'."""
    boxes = [e.get("bbox_normalized") for _, e in entries]
    if not all(boxes):
        return "v"
    cx = [b["Left"] + b["Width"] / 2 for b in boxes]
    cy = [b["Top"] + b["Height"] / 2 for b in boxes]
    return "h" if (max(cx) - min(cx)) > (max(cy) - min(cy)) else "v"


def _cross_offset(free, align):
    """Offset of a panel along the cross-axis given the leftover space `free`."""
    if align == "left":
        return 0
    if align == "right":
        return free
    return free // 2  # center


def _stitch_pngs(entries, orientation, gap=_STITCH_GAP_PX, align="center"):
    """
    Concatenate the already-cropped PNGs in the requested orientation
    ('h' = side by side, 'v' = stacked) WITHOUT resizing them — each panel keeps
    its native pixel size. A white gap of `gap` px is inserted between panels.
    The canvas takes the max height (horizontal) or max width (vertical) as its
    cross-axis size, and smaller panels are aligned on that cross-axis per
    `align` (left/center/right) on a white background. For horizontal layout the
    cross-axis is vertical, so left/center/right map to top/middle/bottom.
    Returns a greyscale PIL image.
    """
    from PIL import Image

    imgs = [_load_panel_image(e["path"]) for _, e in entries]

    total_gap = gap * (len(imgs) - 1)

    if orientation == "h":
        canvas_h = max(im.height for im in imgs)
        canvas_w = sum(im.width for im in imgs) + total_gap
        canvas = Image.new("L", (canvas_w, canvas_h), 255)
        x = 0
        for i, im in enumerate(imgs):
            if i > 0:
                x += gap
            canvas.paste(im, (x, _cross_offset(canvas_h - im.height, align)))
            x += im.width
    else:
        canvas_w = max(im.width for im in imgs)
        canvas_h = sum(im.height for im in imgs) + total_gap
        canvas = Image.new("L", (canvas_w, canvas_h), 255)
        y = 0
        for i, im in enumerate(imgs):
            if i > 0:
                y += gap
            canvas.paste(im, (_cross_offset(canvas_w - im.width, align), y))
            y += im.height

    return canvas


def _build_merged_image(entries, orientation=None, gap=_STITCH_GAP_PX, align="center"):
    """
    Build the merged image.

    - orientation 'horizontal' / 'vertical': stitch the crops in that layout
      (explicit user choice), with a `gap` px white margin between panels and the
      panels `align`ed (left/center/right) on the cross-axis.
    - orientation None / 'auto': re-crop the union region from the PDF when the
      panels share a page (preserves true layout), else stitch using the
      geometry-inferred orientation.

    A `gap` px white border is also added around the whole merged image.
    """
    if orientation in ("horizontal", "vertical"):
        img = _stitch_pngs(entries, "h" if orientation == "horizontal" else "v", gap, align)
    else:
        img = _recrop_union_from_pdf(entries)
        if img is None:
            img = _stitch_pngs(entries, _decide_orientation(entries), gap, align)

    return _grey_png_buffer(_pad_image(img, gap))


# -----------------------------------------------------------------------------
# Routes
# -----------------------------------------------------------------------------

# serves the review UI
@app.route('/')
def index():
    return send_from_directory(BASE_DIR, 'image-view-tester.html')


# serves the merge-notes presentation
@app.route('/notes')
def notes():
    return send_from_directory(BASE_DIR, 'merge-notes.html')


# serves cropped figure PNGs — prefer freshly generated/merged images, then
# fall back to the committed set bundled with the deployment
@app.route('/images/<path:filename>')
def get_image(filename):
    if os.path.exists(os.path.join(IMAGES_OUT, filename)):
        return send_from_directory(IMAGES_OUT, filename)
    if os.path.exists(os.path.join(IMAGES_SRC, filename)):
        return send_from_directory(IMAGES_SRC, filename)
    abort(404)


# returns the full state dict
@app.route('/state')
def get_state():
    # Always prefer the saved working state so merges/edits survive a page
    # reload. _load_state() reads the writable copy first, then the committed
    # snapshot, and only re-extracts from the PDF when no state exists yet.
    # (Use the Reset button to discard merges and re-extract from scratch.)
    return jsonify(_load_state())


# combine two or more extracted images into a single figure
@app.route('/merge', methods=['POST'])
def merge():
    data = request.get_json(silent=True) or {}
    raw_keys = data.get("keys") or []
    orientation = data.get("orientation")  # 'horizontal' | 'vertical' | None (auto)
    if orientation not in (None, "horizontal", "vertical"):
        return jsonify({"error": "orientation must be 'horizontal' or 'vertical'"}), 400

    gap = data.get("gap", _STITCH_GAP_PX)
    try:
        gap = int(gap)
    except (TypeError, ValueError):
        return jsonify({"error": "gap must be an integer number of pixels"}), 400
    if gap < 0 or gap > _STITCH_GAP_MAX_PX:
        return jsonify({"error": f"gap must be between 0 and {_STITCH_GAP_MAX_PX} px"}), 400

    align = data.get("align", "center")
    if align not in ("left", "center", "right"):
        return jsonify({"error": "align must be 'left', 'center' or 'right'"}), 400

    # de-duplicate while preserving order
    seen = set()
    keys = [k for k in raw_keys if not (k in seen or seen.add(k))]
    if len(keys) < 2:
        return jsonify({"error": "merge requires at least two distinct image keys"}), 400

    # The client sends the panels it wants to merge (path + page + bbox + callouts)
    # so this endpoint is fully stateless: it never reads or writes server-side
    # state. That keeps behaviour identical across local and serverless hosts,
    # where /tmp is per-instance and doesn't survive between requests.
    panels = data.get("panels") or {}
    if not panels:
        # back-compat: fall back to any server-side state if the client didn't
        # send panel data
        panels = _load_state()
    missing = [k for k in keys if k not in panels]
    if missing:
        return jsonify({"error": f"unknown image keys: {missing}"}), 400

    # order panels by reading order (page, then top, then left) so the merged
    # image reads A -> B -> ... and the top-left panel becomes the representative
    entries = [(k, panels[k]) for k in keys]

    def reading_order(item):
        e = item[1]
        bb = e.get("bbox_normalized") or {}
        return (e.get("page", 0), bb.get("Top", 0), bb.get("Left", 0))

    entries.sort(key=reading_order)
    ordered_keys = [k for k, _ in entries]
    rep_key = ordered_keys[0]

    try:
        buf = _build_merged_image(entries, orientation, gap, align)
    except Exception as exc:  # pragma: no cover - defensive
        return jsonify({"error": f"could not build merged image: {exc}"}), 500

    # return the merged image inline so the client never has to fetch it back
    # from a (possibly different) serverless instance
    import base64
    data_url = "data:image/png;base64," + base64.b64encode(buf.read()).decode("ascii")

    # build the merged entry from the representative, with the union bbox (when the
    # panels share a page) and the combined callout labels from every panel
    merged_entry = dict(panels[rep_key])
    merged_entry["path"] = data_url
    if _same_page(entries):
        merged_entry["bbox_normalized"] = _union_bbox(entries)

    combined_callouts = {}
    for _, e in entries:
        for num, label in (e.get("callout_numbers") or {}).items():
            combined_callouts[num] = label
    merged_entry["callout_numbers"] = combined_callouts
    merged_entry["merged_from"] = ordered_keys

    removed_keys = [k for k in ordered_keys if k != rep_key]

    return jsonify({
        "merged_key": rep_key,
        "removed_keys": removed_keys,
        "image": merged_entry,
    })


# discard merges/edits and restore the original extracted state
@app.route('/reset', methods=['POST'])
def reset():
    _remove_merged_images()
    return jsonify(_fresh_state())


if __name__ == '__main__':
    app.run(port=5000, debug=True)
