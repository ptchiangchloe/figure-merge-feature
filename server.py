from flask import Flask, jsonify, send_from_directory
from flask_cors import CORS
import json
from document_parser import extract_figures

app = Flask(__name__)
CORS(app)

# serves the review UI
@app.route('/')
def index():
    return send_from_directory('.', 'image-view-tester.html')

# serves cropped figure PNGs from the ./images directory
@app.route('/images/<filename>')
def get_image(filename):
    return send_from_directory('./images', filename)

# runs extraction on every call — re-parses the PDF + Textract response
# and returns the full state dict; no caching
@app.route('/state')
def get_state():
    state = extract_figures("article.pdf", "analyzeDocResponse.json")
    return jsonify(state)

# TODO: merge endpoint — combine two extracted image entries into one
# @app.route('/merge', methods=['POST'])

if __name__ == '__main__':
    app.run(port=5000, debug=True)