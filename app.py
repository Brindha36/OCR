from flask import Flask, render_template, request, jsonify, send_from_directory
import os
import json
import re
import time
import pymysql
from datetime import datetime
from werkzeug.utils import secure_filename
from google import genai
from google.genai import types

app = Flask(__name__)

# Ensure absolute path for storage
BASE_DIR = os.path.abspath(os.path.dirname(__file__))
INVOICE_STORAGE_FOLDER = os.path.join(BASE_DIR, "uploaded_invoices")
os.makedirs(INVOICE_STORAGE_FOLDER, exist_ok=True)
app.config["UPLOAD_FOLDER"] = INVOICE_STORAGE_FOLDER

DB_CONFIG = {
    "host": os.environ.get("DB_HOST"),
    "user": os.environ.get("DB_USER"),
    "password": os.environ.get("DB_PASSWORD"),
    "database": os.environ.get("DB_NAME", "invoice_db"),
    "port": int(os.environ.get("DB_PORT", 4000)),
    "connect_timeout": 10,
    "ssl": {"ssl_mode": "REQUIRED"},
    "cursorclass": pymysql.cursors.DictCursor
}

def get_db_connection():
    return pymysql.connect(**DB_CONFIG)

def get_client():
    api_key = os.getenv("GOOGLE_API_KEY")
    if not api_key:
        raise Exception("GOOGLE_API_KEY is not set in Render Environment variables")
    return genai.Client(api_key=api_key)

def clean_float(val):
    if not val or val == "Not found":
        return 0.0
    if isinstance(val, (int, float)):
        return float(val)
    cleaned = str(val).replace(",", "").strip()
    match = re.search(r"[-+]?\d*\.\d+|\d+", cleaned)
    if match:
        try:
            return float(match.group(0))
        except ValueError:
            return 0.0
    return 0.0

def analyze_invoice(file_path):
    client = get_client()
    uploaded_ref = client.files.upload(file=file_path)

    prompt = """
You are an expert Indian GST tax invoice auditor. Carefully inspect this document and extract the exact details into valid JSON.

JSON Structure:
{
  "company_name": "Seller / Supplier / Vendor Name",
  "invoice_no": "Invoice or Bill Number",
  "date": "YYYY-MM-DD or DD/MM/YYYY",
  "gst_no": "15-digit GSTIN of seller (e.g. 33AAAAA0000A1Z5)",
  "gst_percentage": "Rate percentage like 5%, 12%, 18%, or Multiple",
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
- "net_amount": Total taxable value BEFORE tax.
- "cgst_amount", "sgst_amount", "igst_amount": Respective tax values.
- "gst_5_amount", "gst_12_amount": Specific tax slabs if specified.
- "total_gst_amount": Total tax sum.
- "grand_total": Final payable total.
- Return pure JSON only without backticks or markdown fences.
"""

    # Model priority chain to handle temporary 503 high-demand surges
    candidate_models = ["gemini-2.5-flash", "gemini-2.0-flash", "gemini-1.5-flash"]
    response = None
    last_error = None

    for model_name in candidate_models:
        for attempt in range(2):  # Try twice per model with a short pause
            try:
                response = client.models.generate_content(
                    model=model_name,
                    contents=[uploaded_ref, prompt],
                    config=types.GenerateContentConfig(
                        response_mime_type="application/json",
                        temperature=0.1
                    )
                )
                if response and response.text:
                    break
            except Exception as err:
                last_error = err
                err_str = str(err)
                # If overloaded (503 / 429), pause briefly before retry
                if "503" in err_str or "429" in err_str or "UNAVAILABLE" in err_str:
                    time.sleep(1.5)
                    continue
                else:
                    break
        if response and response.text:
            break

    if not response or not response.text:
        raise Exception(f"AI Service busy across models: {last_error}")

    raw_text = response.text.strip()
    if raw_text.startswith("```"):
        raw_text = raw_text.strip("`")
        if raw_text.startswith("json"):
            raw_text = raw_text[4:].strip()

    try:
        data = json.loads(raw_text)
        for key in ["net_amount", "cgst_amount", "sgst_amount", "igst_amount",
                    "gst_5_amount", "gst_12_amount", "total_gst_amount", "grand_total"]:
            data[key] = f"{clean_float(data.get(key, 0.0)):.2f}"
        return data
    except Exception as e:
        print(f"JSON Parse Exception: {e}")
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

@app.route("/invoices/<filename>")
@app.route("/uploaded_files/<filename>")
def get_invoice_file(filename):
    return send_from_directory(app.config["UPLOAD_FOLDER"], filename)

@app.route("/")
def index():
    batches = []
    try:
        conn = get_db_connection()
        with conn.cursor() as cursor:
            cursor.execute("SELECT * FROM batch_uploads ORDER BY id DESC")
            batches = cursor.fetchall()
        conn.close()
    except Exception as e:
        print(f"Database warning: {e}")
    return render_template("index.html", batches=batches)

@app.route("/analyze", methods=["POST"])
def analyze():
    if "invoice" not in request.files:
        return jsonify({"success": False, "error": "No file uploaded"}), 400

    file = request.files["invoice"]
    if file.filename == "":
        return jsonify({"success": False, "error": "No file selected"}), 400

    try:
        original_name = secure_filename(file.filename)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        saved_filename = f"{timestamp}_{original_name}"
        file_path = os.path.join(app.config["UPLOAD_FOLDER"], saved_filename)
        file.save(file_path)

        data = analyze_invoice(file_path)
        data["filename"] = saved_filename
        return jsonify({"success": True, "data": data})
    except Exception as e:
        print(f"Server Error during analyze: {e}")
        return jsonify({"success": False, "error": f"Analysis failed: {str(e)}"}), 200

@app.route("/save-batch", methods=["POST"])
def save_batch():
    payload = request.json or {}
    records = payload.get("invoices", [])
    staff_name = payload.get("staff_name", "Staff User")
    batch_name = payload.get("batch_name", "General Batch")

    if not records:
        return jsonify({"success": False, "error": "No invoices to save"}), 400

    b_net = sum(clean_float(r.get("net_amount")) for r in records)
    b_cgst = sum(clean_float(r.get("cgst_amount")) for r in records)
    b_sgst = sum(clean_float(r.get("sgst_amount")) for r in records)
    b_igst = sum(clean_float(r.get("igst_amount")) for r in records)
    b_5 = sum(clean_float(r.get("gst_5_amount")) for r in records)
    b_12 = sum(clean_float(r.get("gst_12_amount")) for r in records)
    b_gst = sum(clean_float(r.get("total_gst_amount")) for r in records)
    b_grand = sum(clean_float(r.get("grand_total")) for r in records)

    conn = None
    try:
        conn = get_db_connection()
        with conn.cursor() as cursor:
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

        return jsonify({"success": True, "batch_id": batch_id, "count": len(records)})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 200
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5050))
    app.run(host="0.0.0.0", port=port)
