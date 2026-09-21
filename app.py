from flask import Flask, render_template, request, jsonify, send_from_directory
import os
import json
import pymysql
from PIL import Image
from pdf2image import convert_from_path
from google import genai

app = Flask(__name__)

UPLOAD_FOLDER = "uploads"
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
app.config["UPLOAD_FOLDER"] = UPLOAD_FOLDER

# Dynamic Database Configuration using Environment Variables
DB_CONFIG = {
    "host": os.environ.get("DB_HOST", "uniformkart.com"),
    "user": os.environ.get("DB_USER", "uniformk_sowmya"),
    "password": os.environ.get("DB_PASSWORD", ""),
    "database": os.environ.get("DB_NAME", "uniformk_sowmya"),
    "port": int(os.environ.get("DB_PORT", 3306)),
    "cursorclass": pymysql.cursors.DictCursor
}

def get_db_connection():
    return pymysql.connect(**DB_CONFIG)

def get_client():
    api_key = os.getenv("GOOGLE_API_KEY")
    if not api_key:
        raise Exception("GOOGLE_API_KEY not set in environment")
    return genai.Client(api_key=api_key)

def get_model(client):
    try:
        models = client.models.list()
        for m in models:
            if "gemini" in m.name:
                return m.name
    except Exception:
        pass
    return "gemini-2.5-flash"

def load_file(path):
    if path.lower().endswith(".pdf"):
        # On Render / Linux, poppler is in system PATH; on Windows it uses the local path
        if os.name == "nt":
            pages = convert_from_path(
                path,
                poppler_path=r"C:\poppler\Library\bin"
            )
        else:
            pages = convert_from_path(path)
        return pages[0]
    return Image.open(path)

def clean_float(val):
    if not val or val == "Not found":
        return 0.0
    try:
        cleaned = "".join(c for c in str(val) if c.isdigit() or c == ".")
        return float(cleaned) if cleaned else 0.0
    except ValueError:
        return 0.0

def analyze_invoice(file_path):
    client = get_client()
    model = get_model(client)
    img = load_file(file_path)

    prompt = """
Extract invoice details and return ONLY a valid JSON object:
{
  "company_name": "",
  "invoice_no": "",
  "date": "",
  "gst_no": "",
  "gst_percentage": "",
  "net_amount": "0.00",
  "cgst_amount": "0.00",
  "sgst_amount": "0.00",
  "igst_amount": "0.00",
  "gst_5_amount": "0.00",
  "gst_12_amount": "0.00",
  "total_gst_amount": "0.00",
  "grand_total": "0.00",
  "type": "Manual or Computer Generated"
}

Rules:
- Return ONLY plain valid JSON without markdown fences.
- Extract numbers cleanly (e.g. 1500.00). If not applicable, return "0.00".
- "net_amount" is taxable amount before GST.
"""

    response = client.models.generate_content(
        model=model,
        contents=[prompt, img]
    )

    raw_text = response.text.strip()
    if raw_text.startswith("```"):
        raw_text = raw_text.strip("`")
        if raw_text.startswith("json"):
            raw_text = raw_text[4:].strip()

    try:
        return json.loads(raw_text)
    except Exception:
        return {
            "company_name": "Not found",
            "invoice_no": "Not found",
            "date": "Not found",
            "gst_no": "Not found",
            "gst_percentage": "Not found",
            "net_amount": "0.00",
            "cgst_amount": "0.00",
            "sgst_amount": "0.00",
            "igst_amount": "0.00",
            "gst_5_amount": "0.00",
            "gst_12_amount": "0.00",
            "total_gst_amount": "0.00",
            "grand_total": "0.00",
            "type": "Not found"
        }

@app.route("/uploads/<filename>")
def uploaded_file(filename):
    return send_from_directory(app.config["UPLOAD_FOLDER"], filename)

@app.route("/")
def index():
    conn = get_db_connection()
    with conn.cursor() as cursor:
        cursor.execute("SELECT * FROM batch_uploads ORDER BY id DESC")
        batches = cursor.fetchall()
    conn.close()
    return render_template("index.html", batches=batches)

@app.route("/analyze", methods=["POST"])
def analyze():
    if "invoice" not in request.files:
        return jsonify({"success": False, "error": "No file uploaded"}), 400

    file = request.files["invoice"]
    if file.filename == "":
        return jsonify({"success": False, "error": "No file selected"}), 400

    filename = file.filename
    path = os.path.join(UPLOAD_FOLDER, filename)
    file.save(path)

    data = analyze_invoice(path)
    data["filename"] = filename
    return jsonify({"success": True, "data": data})

@app.route("/save-batch", methods=["POST"])
def save_batch():
    payload = request.json
    records = payload.get("invoices", [])
    staff_name = payload.get("staff_name", "Staff User")
    batch_name = payload.get("batch_name", "General Batch")

    if not records:
        return jsonify({"success": False, "error": "No invoices to save"}), 400

    # Calculate master summary totals
    b_net = sum(clean_float(r.get("net_amount")) for r in records)
    b_cgst = sum(clean_float(r.get("cgst_amount")) for r in records)
    b_sgst = sum(clean_float(r.get("sgst_amount")) for r in records)
    b_igst = sum(clean_float(r.get("igst_amount")) for r in records)
    b_5 = sum(clean_float(r.get("gst_5_amount")) for r in records)
    b_12 = sum(clean_float(r.get("gst_12_amount")) for r in records)
    b_gst = sum(clean_float(r.get("total_gst_amount")) for r in records)
    b_grand = sum(clean_float(r.get("grand_total")) for r in records)

    conn = get_db_connection()
    try:
        with conn.cursor() as cursor:
            # 1. Insert into Master Table (batch_uploads)
            master_sql = """
                INSERT INTO batch_uploads (
                    batch_name, staff_name, total_invoices, batch_net_total,
                    batch_cgst_total, batch_sgst_total, batch_igst_total,
                    batch_gst_5_total, batch_gst_12_total, batch_total_gst, batch_grand_total
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """
            cursor.execute(master_sql, (
                batch_name, staff_name, len(records), b_net,
                b_cgst, b_sgst, b_igst, b_5, b_12, b_gst, b_grand
            ))
            batch_id = cursor.lastrowid

            # 2. Insert into Detail Table (invoice_items) linked by batch_id
            detail_sql = """
                INSERT INTO invoice_items (
                    batch_id, filename, company_name, invoice_no, invoice_date, gst_no,
                    gst_percentage, net_amount, cgst_amount, sgst_amount, igst_amount,
                    gst_5_amount, gst_12_amount, total_gst_amount, grand_total, invoice_type
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """
            detail_rows = [
                (
                    batch_id,
                    r.get("filename", ""),
                    r.get("company_name", "Not found"),
                    r.get("invoice_no", "Not found"),
                    r.get("date", "Not found"),
                    r.get("gst_no", "Not found"),
                    r.get("gst_percentage", "Not found"),
                    clean_float(r.get("net_amount")),
                    clean_float(r.get("cgst_amount")),
                    clean_float(r.get("sgst_amount")),
                    clean_float(r.get("igst_amount")),
                    clean_float(r.get("gst_5_amount")),
                    clean_float(r.get("gst_12_amount")),
                    clean_float(r.get("total_gst_amount")),
                    clean_float(r.get("grand_total")),
                    r.get("type", "Not found")
                )
                for r in records
            ]
            cursor.executemany(detail_sql, detail_rows)
            conn.commit()

        conn.close()
        return jsonify({"success": True, "batch_id": batch_id, "count": len(records)})
    except Exception as e:
        conn.close()
        return jsonify({"success": False, "error": str(e)}), 500


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5050))
    app.run(host="0.0.0.0", port=port)
