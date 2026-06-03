# standard library
import json
import os
from pathlib import Path
from io import BytesIO
from dataclasses import dataclass
from typing import Optional

# third party
import fitz          # pip install pymupdf
from PIL import Image  # pip install pillow


# -----------------------------------------------------------------------------
# Internal helpers 
# -----------------------------------------------------------------------------

@dataclass
class _PdfAwsMergedFigure:
    """
    Represents a merged figure bounding box, formed by clustering one or more
    raw LAYOUT_FIGURE Textract blocks that belong to the same logical figure.
    All coords are normalized (0..1) page fractions.
    """
    page: int
    left: float
    top: float
    right: float
    bottom: float
    source_ids: list
    representative_id: str

    def to_textract_bb(self) -> dict:
        return {
            "Left": self.left,
            "Top": self.top,
            "Width": self.right - self.left,
            "Height": self.bottom - self.top,
        }


@dataclass
class _PdfAwsFigureBBox:
    """
    Thin wrapper around a single raw Textract LAYOUT_FIGURE block,
    converting Left/Top/Width/Height into explicit left/top/right/bottom
    for easier overlap and gap arithmetic.
    """
    block_id: str
    page: int
    left: float
    top: float
    right: float
    bottom: float

    @classmethod
    def from_textract_block(cls, block: dict) -> "_PdfAwsFigureBBox":
        bb = block["Geometry"]["BoundingBox"]
        return cls(
            block_id=block["Id"],
            page=block["Page"],
            left=bb["Left"],
            top=bb["Top"],
            right=bb["Left"] + bb["Width"],
            bottom=bb["Top"] + bb["Height"],
        )


# ---- Proximity thresholds for clustering nearby LAYOUT_FIGURE blocks ----
# Two figure fragments are merged if their horizontal AND vertical gaps
# are both within these fractions of the page. Separate values for
# single-column vs two-column layouts because two-column pages have
# figures that sit closer to text/other figures horizontally.
_pdf_aws_fc_THRESH_X_SINGLE_COL = 0.08
_pdf_aws_fc_THRESH_Y_SINGLE_COL = 0.08
_pdf_aws_fc_THRESH_X_TWO_COL = 0.08
_pdf_aws_fc_THRESH_Y_TWO_COL = 0.08

# Block types that act as hard separators in reading order.
# If a block of one of these types sits between two LAYOUT_FIGURE blocks
# in the page's top-level CHILD list, we refuse to merge those figures
# even if they are spatially close — the text/table/etc between them
# means they are logically distinct.
_pdf_aws_fc_PAGE_ORDER_SEPARATOR_TYPES = frozenset({
    "LAYOUT_TEXT",
    "LAYOUT_TITLE",
    "LAYOUT_SECTION_HEADER",
    "LAYOUT_LIST",
    "LAYOUT_TABLE",
    "LAYOUT_KEY_VALUE",
})

def _pdf_aws_fc_page_order_blocks_clubbing(
    order_ids: list,
    block_map: dict,
    figure_id_a: str,
    figure_id_b: str,
) -> bool:
    """
    Return True (block the merge) if a separator-type layout block appears
    strictly between figure_id_a and figure_id_b in the page's reading-order
    CHILD list.  Prevents merging figures that are spatially close but have
    body text or a table sandwiched between them in document order.
    """
    if not order_ids:
        return False
    try:
        ia = order_ids.index(figure_id_a)
        ib = order_ids.index(figure_id_b)
    except ValueError:
        return False
    lo, hi = (ia, ib) if ia < ib else (ib, ia)
    for mid_id in order_ids[lo + 1 : hi]:
        ob = block_map.get(mid_id)
        if ob and ob.get("BlockType") in _pdf_aws_fc_PAGE_ORDER_SEPARATOR_TYPES:
            return True
    return False


def _pdf_aws_fc_detect_two_column(blocks: list, page: int) -> bool:
    """
    Heuristic: decide if a page is two-column by looking at the horizontal
    centres of all LAYOUT_TEXT blocks on that page.  If >60% of text blocks
    fall in the left (<0.45) or right (>0.55) column bands, treat the page
    as two-column and use the wider proximity thresholds.
    """
    centres = []
    for b in blocks:
        if b.get("BlockType") != "LAYOUT_TEXT":
            continue
        if b.get("Page") != page:
            continue
        bb = b["Geometry"]["BoundingBox"]
        centres.append(bb["Left"] + bb["Width"] / 2)
    if len(centres) < 4:
        return False
    left_col = sum(1 for c in centres if c < 0.45)
    right_col = sum(1 for c in centres if c > 0.55)
    column_ratio = (left_col + right_col) / len(centres)
    return column_ratio > 0.60

def pil_image_to_greyscale_png_buffer(img):
    """
    Normalise any PIL image to an 8-bit greyscale PNG and return it as a
    seeked BytesIO buffer.

    Handles transparent modes (RGBA, LA, P) by compositing onto a white
    background before converting to greyscale, so transparency doesn't
    produce black artefacts.  Used uniformly for PDF crops, PPT extractions,
    DOCX extractions, and EMF→PNG conversions.
    """

    if img.mode in ("RGBA", "LA", "P"):
        bg = Image.new("RGB", img.size, (255, 255, 255))
        if img.mode == "P":
            img = img.convert("RGBA")
        if img.mode in ("RGBA", "LA"):
            bg.paste(img, mask=img.split()[-1])
        else:
            bg.paste(img.convert("RGB"))
        img = bg
    elif img.mode not in ("L", "1"):
        img = img.convert("RGB")
    if img.mode != "L":
        img = img.convert("L")
    buf = BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return buf

def _pdf_aws_fc_dfs(node: int, graph: dict, visited: set, cluster: list):
    """Standard iterative-free DFS used to collect all nodes in a connected component."""
    visited.add(node)
    cluster.append(node)
    for neighbour in graph[node]:
        if neighbour not in visited:
            _pdf_aws_fc_dfs(neighbour, graph, visited, cluster)


def _pdf_aws_fc_page_top_level_child_ids(blocks: list, page: int):
    """
    Return the ordered list of top-level CHILD block IDs from the PAGE block
    for the given 1-based page number.  This represents Textract's reading
    order for the page and is used by the separator check above.
    Returns None if no PAGE block is found for that page.
    """
    for b in blocks:
        if b.get("BlockType") != "PAGE":
            continue
        if b.get("Page") != page:
            continue
        for rel in b.get("Relationships") or []:
            if rel.get("Type") == "CHILD":
                return list(rel.get("Ids") or [])
    return None



# ---- Figure quality filters ----
# Drop figures that are too small (likely logos/watermarks/decorative icons)
# or have extreme aspect ratios (likely horizontal rules or thin banners).
_pdf_aws_MIN_SIZE_FRAC = 0.02   # skip if width or height < 2% of page
_pdf_aws_MAX_ASPECT_RATIO = 10.0  # skip if width >= 10*height or height >= 10*width

def _pdf_aws_bbox_norm_to_pixels(bbox, img_w, img_h):
    """Convert a Textract-style normalized bbox to absolute pixel coords for PIL.crop()."""
    left = bbox["Left"] * img_w
    top = bbox["Top"] * img_h
    right = (bbox["Left"] + bbox["Width"]) * img_w
    bottom = (bbox["Top"] + bbox["Height"]) * img_h
    return (int(left), int(top), int(right), int(bottom))

def _pdf_aws_should_skip_figure_by_bbox(bbox, page_width_frac=1.0, page_height_frac=1.0):
    """
    Return True if the figure should be discarded.
    Filters out zero-size, sub-2% micro-figures, and extreme aspect ratios
    that are almost certainly decorative elements rather than real figures.
    """
    w = bbox.get("Width", 0)
    h = bbox.get("Height", 0)
    if w <= 0 or h <= 0:
        return True
    if w < _pdf_aws_MIN_SIZE_FRAC or h < _pdf_aws_MIN_SIZE_FRAC:
        return True
    if w / h >= _pdf_aws_MAX_ASPECT_RATIO or h / w >= _pdf_aws_MAX_ASPECT_RATIO:
        return True
    return False

def _pdf_aws_fc_merge_cluster(bboxes: list, cluster: list) -> _PdfAwsMergedFigure:
    """
    Collapse a list of bbox indices (one connected component) into a single
    _PdfAwsMergedFigure whose bbox is the union of all member bboxes.
    The representative_id is the top-left-most member (lowest top, then left).
    """
    members = [bboxes[i] for i in cluster]
    rep = min(members, key=lambda b: (b.top, b.left))
    return _PdfAwsMergedFigure(
        page=members[0].page,
        left=min(b.left for b in members),
        top=min(b.top for b in members),
        right=max(b.right for b in members),
        bottom=max(b.bottom for b in members),
        source_ids=[b.block_id for b in members],
        representative_id=rep.block_id,
    )

def _pdf_aws_fc_should_merge(a: _PdfAwsFigureBBox, b: _PdfAwsFigureBBox, thresh_x: float, thresh_y: float) -> bool:
    """
    Return True if two figure bboxes are close enough to be the same logical figure.
    Uses the gap between the boxes (0 if they overlap) rather than centre distance,
    so partial overlaps always merge and proximity is measured edge-to-edge.
    """
    horiz_gap = max(0.0, max(a.left, b.left) - min(a.right, b.right))
    vert_gap = max(0.0, max(a.top, b.top) - min(a.bottom, b.bottom))
    return horiz_gap <= thresh_x and vert_gap <= thresh_y


def _pdf_aws_fc_cluster_figures(
    bboxes: list,
    thresh_x: float,
    thresh_y: float,
    page_order_ids: Optional[list],
    block_map: dict,
) -> list:
    """
    Build an adjacency graph over all figure bboxes on a single page and
    return connected components (each component = one logical figure).

    Two nodes are connected if:
      1. Their edge-to-edge gap is within thresh_x / thresh_y, AND
      2. No separator-type layout block sits between them in reading order.

    Returns a list of clusters, where each cluster is a list of bbox indices.
    """
    n = len(bboxes)
    graph = {i: [] for i in range(n)}
    for i in range(n):
        for j in range(i + 1, n):
            if not _pdf_aws_fc_should_merge(bboxes[i], bboxes[j], thresh_x, thresh_y):
                continue
            if page_order_ids and _pdf_aws_fc_page_order_blocks_clubbing(
                page_order_ids,
                block_map,
                bboxes[i].block_id,
                bboxes[j].block_id,
            ):
                continue
            graph[i].append(j)
            graph[j].append(i)
    visited = set()
    clusters = []
    for i in range(n):
        if i not in visited:
            cluster = []
            _pdf_aws_fc_dfs(i, graph, visited, cluster)
            clusters.append(cluster)
    return clusters

def group_layout_figures_textract(
    textract_output: dict,
    two_column_pages=None,
    auto_detect_columns: bool = True,
) -> dict:
    """
    Top-level clustering entry point.  Reads all LAYOUT_FIGURE blocks from a
    Textract response and returns a page-keyed dict of merged figures.

    Args:
        textract_output:    Raw Textract AnalyzeDocument/DetectDocumentText response.
        two_column_pages:   Optional set of 1-based page numbers to force two-column
                            thresholds; if None and auto_detect_columns is True,
                            layout is inferred automatically per page.
        auto_detect_columns: When True and two_column_pages is None, use
                            _pdf_aws_fc_detect_two_column() per page.

    Returns:
        dict mapping 1-based page int → list of _PdfAwsMergedFigure.
    """
    blocks = textract_output["Blocks"]
    block_map = {b["Id"]: b for b in blocks if b.get("Id")}
    figures_by_page = {}
    for block in blocks:
        if block.get("BlockType") != "LAYOUT_FIGURE":
            continue
        bbox = _PdfAwsFigureBBox.from_textract_block(block)
        figures_by_page.setdefault(bbox.page, []).append(bbox)

    result = {}
    for page, bboxes in figures_by_page.items():
        if two_column_pages is not None:
            is_two_col = page in two_column_pages
        elif auto_detect_columns:
            is_two_col = _pdf_aws_fc_detect_two_column(blocks, page)
        else:
            is_two_col = False
        thresh_x = _pdf_aws_fc_THRESH_X_TWO_COL if is_two_col else _pdf_aws_fc_THRESH_X_SINGLE_COL
        thresh_y = _pdf_aws_fc_THRESH_Y_TWO_COL if is_two_col else _pdf_aws_fc_THRESH_Y_SINGLE_COL
        page_order_ids = _pdf_aws_fc_page_top_level_child_ids(blocks, page)
        clusters = _pdf_aws_fc_cluster_figures(
            bboxes, thresh_x, thresh_y, page_order_ids, block_map
        )
        result[page] = [_pdf_aws_fc_merge_cluster(bboxes, c) for c in clusters]
    return result

def _pdf_aws_extract_figures_clustered(pdf_bytes, textract_response: dict, dpi=200):
    """
    Core extraction routine: cluster Textract LAYOUT_FIGURE blocks, then for
    each cluster rasterize the union bbox from the PDF page at the given DPI
    and return the crop as a greyscale PNG.

    Args:
        pdf_bytes:          Raw PDF bytes.
        textract_response:  Full Textract response dict.
        dpi:                Rasterization resolution (default 200 dpi).

    Returns:
        Ordered dict: representative_id → {
            "png":              BytesIO greyscale PNG (seeked to 0),
            "page":             1-based page number,
            "dpi":              rasterization DPI used,
            "page_width_px":    full-page pixmap width in pixels,
            "page_height_px":   full-page pixmap height in pixels,
            "bbox_normalized":  Textract-style { Left, Top, Width, Height } in 0..1 fractions,
        }
        Insertion order matches extraction order (top-left figure first, page by page).
    """
    import fitz
    from PIL import Image

    merged_by_page = group_layout_figures_textract(textract_response)
    figure_payloads = {}
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    try:
        for page_num_1idx, merged_list in merged_by_page.items():
            if page_num_1idx < 1 or page_num_1idx > len(doc):
                continue
            page = doc[page_num_1idx - 1]
            zoom = dpi / 72.0
            mat = fitz.Matrix(zoom, zoom)
            pix = page.get_pixmap(matrix=mat, alpha=False)
            img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
            img_w, img_h = img.size
            for mf in merged_list:
                bb = mf.to_textract_bb()
                if _pdf_aws_should_skip_figure_by_bbox(bb):
                    continue
                crop_box = _pdf_aws_bbox_norm_to_pixels(bb, img_w, img_h)
                crop = img.crop(crop_box)
                figure_payloads[mf.representative_id] = {
                    "png": pil_image_to_greyscale_png_buffer(crop),
                    "page": mf.page,
                    "dpi": dpi,
                    "page_width_px": img_w,
                    "page_height_px": img_h,
                    "bbox_normalized": dict(bb),
                }
    finally:
        doc.close()
    return figure_payloads


# -----------------------------------------------------------------------------
# Entry point for extracting figures 
# -----------------------------------------------------------------------------

def extract_figures(
    pdf_path: str,
    textract_json_path: str,
    output_dir: str = "./images",
    state_json_path: str = "state.json",
):
    state_path = Path(state_json_path)
    previous = {}
    if state_path.exists():
        with open(state_path) as f:
            previous = json.load(f)

    with open(pdf_path, "rb") as f:
        pdf_bytes = f.read()
    with open(textract_json_path) as f:
        textract_response = json.load(f)

    os.makedirs(output_dir, exist_ok=True)
    figure_payloads = _pdf_aws_extract_figures_clustered(pdf_bytes, textract_response)

    state = {}
    for i, (_rep_id, payload) in enumerate(figure_payloads.items(), start=1):
        key = f"EXTRACTED_IMAGE_{i}"
        filename = f"image{i}.png"
        filepath = Path(output_dir) / filename
        buf = payload["png"]
        with open(filepath, "wb") as f:
            f.write(buf.read())

        prev = previous.get(key, {})
        co = prev.get("callout_numbers")
        if not isinstance(co, dict):
            co = {}

        state[key] = {
            "path": filename,
            "captions": prev.get("captions", ""),
            "number": prev.get("number", ""),
            "tag": prev.get("tag", "keep"),
            "readable": prev.get("readable", 1),
            "callout_numbers": dict(co),
            "page": payload["page"],
            "dpi": payload["dpi"],
            "page_width_px": payload["page_width_px"],
            "page_height_px": payload["page_height_px"],
            "bbox_normalized": payload["bbox_normalized"],
        }

    with open(state_path, "w") as f:
        json.dump(state, f, indent=2)

    print(f"Extracted {len(state)} figures → {output_dir}")
    return state


# def merge_figures #TODO