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

def _grey_png_buffer(pil_img):
    """Save any PIL image as an 8-bit greyscale PNG buffer (matches the pipeline)."""
    from PIL import Image
    if pil_img.mode != "L":
        pil_img = pil_img.convert("L")
    buf = BytesIO()
    pil_img.save(buf, format="PNG")
    buf.seek(0)
    return buf


def _union_bbox(entries):
    """Smallest normalized bbox enclosing every panel (entries must share a page)."""
    boxes = [e["bbox_normalized"] for _, e in entries]
    left = min(b["Left"] for b in boxes)
    top = min(b["Top"] for b in boxes)
    right = max(b["Left"] + b["Width"] for b in boxes)
    bottom = max(b["Top"] + b["Height"] for b in boxes)
    return {"Left": left, "Top": top, "Width": right - left, "Height": bottom - top}


def _recrop_union_from_pdf(entries):
    """
    Preferred merge: when all panels are on the same page and carry bbox metadata,
    re-rasterize their union region straight from the PDF. This reconstructs the
    panels' true spatial layout (the same thing the clustering step does when it
    groups fragments), regardless of horizontal/vertical/diagonal arrangement.

    Returns (png_buffer, union_bbox) or None if it can't be done.
    """
    if document_parser is None:
        return None
    if not all(e.get("bbox_normalized") and e.get("page") for _, e in entries):
        return None
    pages = {e["page"] for _, e in entries}
    if len(pages) != 1:
        return None

    try:
        import fitz
        from PIL import Image
    except Exception:
        return None

    page_num = pages.pop()
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

    return _grey_png_buffer(crop), union


def _decide_orientation(entries):
    """Side-by-side ('h') if panels spread more horizontally than vertically, else 'v'."""
    boxes = [e.get("bbox_normalized") for _, e in entries]
    if not all(boxes):
        return "v"
    cx = [b["Left"] + b["Width"] / 2 for b in boxes]
    cy = [b["Top"] + b["Height"] / 2 for b in boxes]
    return "h" if (max(cx) - min(cx)) > (max(cy) - min(cy)) else "v"


def _stitch_pngs(entries):
    """
    Fallback merge: concatenate the already-cropped PNGs. Used for cross-page
    merges or when bbox metadata is unavailable. Panels are normalized to a
    common edge length so the seam lines up.

    Returns (png_buffer, None).
    """
    from PIL import Image

    imgs = []
    for _, e in entries:
        path = _resolve_image_path(e["path"])
        if path is None:
            raise FileNotFoundError(e["path"])
        imgs.append(Image.open(path).convert("L"))

    orientation = _decide_orientation(entries)
    if orientation == "h":
        target_h = max(im.height for im in imgs)
        scaled = [im.resize((max(1, round(im.width * target_h / im.height)), target_h)) for im in imgs]
        canvas = Image.new("L", (sum(im.width for im in scaled), target_h), 255)
        x = 0
        for im in scaled:
            canvas.paste(im, (x, 0))
            x += im.width
    else:
        target_w = max(im.width for im in imgs)
        scaled = [im.resize((target_w, max(1, round(im.height * target_w / im.width)))) for im in imgs]
        canvas = Image.new("L", (target_w, sum(im.height for im in scaled)), 255)
        y = 0
        for im in scaled:
            canvas.paste(im, (0, y))
            y += im.height

    return _grey_png_buffer(canvas), None


def _build_merged_image(entries):
    """Re-crop from the PDF when possible, otherwise stitch the existing crops."""
    result = _recrop_union_from_pdf(entries)
    if result is not None:
        return result
    return _stitch_pngs(entries)


# -----------------------------------------------------------------------------
# Routes
# -----------------------------------------------------------------------------

# serves the review UI
@app.route('/')
def index():
    return send_from_directory(BASE_DIR, 'image-view-tester.html')


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
    # On a read-only serverless filesystem we can't re-run extraction (it writes
    # images + state.json). The repo already ships the extracted output, so serve
    # that instead — identical content, no writes required.
    if ON_SERVERLESS or extract_figures is None:
        path = STATE_OUT if os.path.exists(STATE_OUT) else COMMITTED_STATE
        with open(path) as f:
            return jsonify(json.load(f))

    # Local dev: re-run extraction so parser changes are reflected live.
    state = extract_figures(
        PDF_PATH,
        TEXTRACT_PATH,
        output_dir=IMAGES_OUT,
        state_json_path=STATE_OUT,
    )
    return jsonify(state)


# combine two or more extracted images into a single figure
@app.route('/merge', methods=['POST'])
def merge():
    data = request.get_json(silent=True) or {}
    raw_keys = data.get("keys") or []

    # de-duplicate while preserving order
    seen = set()
    keys = [k for k in raw_keys if not (k in seen or seen.add(k))]
    if len(keys) < 2:
        return jsonify({"error": "merge requires at least two distinct image keys"}), 400

    state = _load_state()
    missing = [k for k in keys if k not in state]
    if missing:
        return jsonify({"error": f"unknown image keys: {missing}"}), 400

    # order panels by reading order (page, then top, then left) so the merged
    # image reads A -> B -> ... and the top-left panel becomes the representative
    entries = [(k, state[k]) for k in keys]

    def reading_order(item):
        e = item[1]
        bb = e.get("bbox_normalized") or {}
        return (e.get("page", 0), bb.get("Top", 0), bb.get("Left", 0))

    entries.sort(key=reading_order)
    ordered_keys = [k for k, _ in entries]
    rep_key = ordered_keys[0]

    try:
        buf, union = _build_merged_image(entries)
    except Exception as exc:  # pragma: no cover - defensive
        return jsonify({"error": f"could not build merged image: {exc}"}), 500

    os.makedirs(IMAGES_OUT, exist_ok=True)
    merged_filename = f"merged_{rep_key}.png"
    with open(os.path.join(IMAGES_OUT, merged_filename), "wb") as f:
        f.write(buf.read())

    # build the merged entry from the representative, with the union bbox and the
    # combined callout labels from every panel
    merged_entry = dict(state[rep_key])
    merged_entry["path"] = merged_filename
    if union is not None:
        merged_entry["bbox_normalized"] = union

    combined_callouts = {}
    for _, e in entries:
        for num, label in (e.get("callout_numbers") or {}).items():
            combined_callouts[num] = label
    merged_entry["callout_numbers"] = combined_callouts
    merged_entry["merged_from"] = ordered_keys

    # rebuild state: replace the representative in place, drop the other panels
    removed_keys = [k for k in ordered_keys if k != rep_key]
    new_state = {}
    for k, v in state.items():
        if k == rep_key:
            new_state[k] = merged_entry
        elif k in removed_keys:
            continue
        else:
            new_state[k] = v

    _save_state(new_state)

    return jsonify({
        "merged_key": rep_key,
        "removed_keys": removed_keys,
        "image": merged_entry,
        "state": new_state,
    })


# discard merges/edits and restore the original extracted state
@app.route('/reset', methods=['POST'])
def reset():
    _remove_merged_images()
    return jsonify(_fresh_state())


if __name__ == '__main__':
    app.run(port=5000, debug=True)
