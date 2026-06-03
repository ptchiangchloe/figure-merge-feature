import os
import json

from flask import Flask, jsonify, send_from_directory, abort
from flask_cors import CORS

# Importing the parser pulls in PyMuPDF. On a read-only serverless host we only
# serve the pre-extracted artifacts, so don't let a missing/heavy dependency
# stop the app from booting.
try:
    from document_parser import extract_figures
except Exception:  # pragma: no cover - serverless fallback
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


# TODO: merge endpoint — combine two extracted image entries into one.
# Writes (merged PNG + updated state) must target IMAGES_OUT / STATE_OUT so they
# land in /tmp on serverless rather than the read-only bundle.
# @app.route('/merge', methods=['POST'])

if __name__ == '__main__':
    app.run(port=5000, debug=True)
