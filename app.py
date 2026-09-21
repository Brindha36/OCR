from flask import Flask, render_template, request, jsonify, send_from_directory
import os
import json
import re
import pymysql
from PIL import Image
from google import genai
from google.genai import types

app = Flask(__name__)

UPLOAD_FOLDER = os.path.join(os.getcwd(), "uploaded_files")
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
app.config["UPLOAD_FOLDER"] = UPLOAD_FOLDER

# Dynamic Database Configuration using Environment Variables
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

def clean_float(val):
    """Safely extracts a valid decimal float number from any string."""
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
    model = get_model(client)

    # Gemini handles both images and PDFs natively without needing Poppler!
    prompt = """
You are an expert Indian GST tax invoice auditor. Carefully inspect the entire document image/PDF and extract the accurate financial values into valid JSON.

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

Extraction Rules:
1. "net_amount": The total TAXABLE amount BEFORE tax/GST is added (Subtotal / Taxable Value).
2. "cgst_amount": Central GST amount (if any; else 0.00).
3. "sgst_amount": State GST amount (if any; else 0.00).
4. "igst_amount": Integrated GST amount (if any; else 0.00).
5. "gst_5_amount": Tax amount specifically charged at 5% GST rate (if itemized; else 0.00).
6. "gst_12_amount": Tax amount specifically charged at 12% GST rate (if itemized; else 0.00).
7. "total_gst_amount": Sum of all GST taxes (CGST + SGST + IGST).
8. "grand_total": FINAL payable invoice amount including all taxes, round-offs, freight, and discounts.
9. Return ONLY valid pure JSON.
"""

    if file_path.lower().endswith(".pdf"):
        with open(file_path, "rb") as f:
            pdf_bytes = f.read()
        file_part = types.Part.from_bytes(data=pdf_bytes, mime_type="application/pdf")
    else:
        img = Image.open(file_path)
        file_part = img

    try:
        response = client.models.generate_content(
            model=model,
            contents=[prompt, file_part],
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                temperature=0.1
            )
        )
        raw_text = response.text.strip()
    except Exception:
        response = client.models.generate_content(
            model=model,
            contents=[prompt, file_part]
        )
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
        print(f"JSON parsing error: {e}")
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

@app.route("/uploaded_files/<filename>")
def uploaded_file(filename):
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
        print(f"Database connection skipped/failed: {e}")
    return render_template("index.html", batches=batches)

@app.route("/analyze", methods=["POST"])
def analyze():
    if "invoice" not in request.files:
        return jsonify({"success": False, "error": "No file uploaded"}), 400

    file = request.files["invoice"]
    if file.filename == "":
        return jsonify({"success": False, "error": "No file selected"}), 400

    try:
        filename = file.filename
        path = os.path.join(UPLOAD_FOLDER, filename)
        file.save(path)

        data = analyze_invoice(path)
        data["filename"] = filename
        return jsonify({"success": True, "data": data})
    except Exception as e:
        print(f"Analysis error: {e}")
        return jsonify({"success": False, "error": str(e)}), 200

@app.route("/save-batch", methods=["POST"])
def save_batch():
    payload = request.json
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
