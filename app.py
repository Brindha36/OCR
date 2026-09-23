from flask import Flask, render_template, request, jsonify, send_from_directory, Response
import os
import io
import csv
import json
import re
import time
import decimal
import pymysql
from datetime import datetime, date
from werkzeug.utils import secure_filename
from google import genai
from google.genai import types

app = Flask(__name__)

# Serialize Decimal and Date values to JSON-safe formats
def serialize_row(obj):
    if isinstance(obj, (datetime, date)):
        return obj.strftime("%Y-%m-%d")
    if isinstance(obj, decimal.Decimal):
        return float(obj)
    return obj

def clean_row(row):
    if not row:
        return {}
    return {k: serialize_row(v) for k, v in row.items()}

# Robust date parser: converts any invoice date string (DD/MM/YYYY, DD-MM-YYYY, YYYY-MM-DD) to a standard date object
def parse_date_to_comparable(val):
    if not val:
        return None
    if isinstance(val, datetime):
        return val.date()
    if isinstance(val, date):
        return val

    s = str(val).strip()
    # Match YYYY-MM-DD
    match_iso = re.search(r"(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})", s)
    if match_iso:
        try:
            return date(int(match_iso.group(1)), int(match_iso.group(2)), int(match_iso.group(3)))
        except ValueError:
            pass

    # Match DD/MM/YYYY or DD-MM-YYYY
    match_dmy = re.search(r"(\d{1,2})[-/.](\d{1,2})[-/.](\d{4})", s)
    if match_dmy:
        try:
            return date(int(match_dmy.group(3)), int(match_dmy.group(2)), int(match_dmy.group(1)))
        except ValueError:
            pass

    return None

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
INVOICE_STORAGE_FOLDER = os.path.join(BASE_DIR, "uploaded_invoices")
os.makedirs(INVOICE_STORAGE_FOLDER, exist_ok=True)
app.config["UPLOAD_FOLDER"] = INVOICE_STORAGE_FOLDER

# TiDB Database Connection
def get_db_connection():
    db_port = os.environ.get("DB_PORT", "4000")
    try:
        port_num = int(db_port)
    except ValueError:
        port_num = 4000

    conn_kwargs = {
        "host": os.environ.get("DB_HOST", "127.0.0.1"),
        "user": os.environ.get("DB_USER", "root"),
        "password": os.environ.get("DB_PASSWORD", ""),
        "database": os.environ.get("DB_NAME") or "invoice_db",
        "port": port_num,
        "connect_timeout": 15,
        "cursorclass": pymysql.cursors.DictCursor
    }

    ssl_env = os.environ.get("DB_SSL", "true").lower()
    if ssl_env in ["true", "1", "required"]:
        conn_kwargs["ssl"] = {"ssl_mode": "REQUIRED"}

    return pymysql.connect(**conn_kwargs)

def get_client():
    api_key = os.getenv("GOOGLE_API_KEY")
    if not api_key:
        raise Exception("GOOGLE_API_KEY environment variable is not set")
    return genai.Client(api_key=api_key)

def get_available_flash_models(client):
    valid_models = []
    try:
        for m in client.models.list():
            name = m.name.replace("models/", "")
            if "flash" in name.lower():
                valid_models.append(name)
    except Exception as e:
        print(f"Model list fallback: {e}")
    if not valid_models:
        valid_models = ["gemini-2.5-flash", "gemini-2.0-flash"]
    return valid_models

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
Analyze the uploaded Indian GST tax invoice document.
Extract all requested fields according to the schema provided.

Classification Instructions for 'type':
- 'Manual': The invoice entries are handwritten (pen, pencil), on a printed bill-book with physical handwritten items/rates, or carbon-copy slips.
- 'Computer Generated': The invoice is generated and printed via software, POS billing printer, ERP, Tally, Zoho, Excel, or digital PDF layout.

Financial Rules:
- 'net_amount' is the taxable subtotal before GST.
- 'grand_total' is the final payable total.
- Convert all numbers to standard decimal format (e.g., 1250.00).
"""

    invoice_schema = {
        "type": "OBJECT",
        "properties": {
            "company_name": {"type": "STRING"},
            "invoice_no": {"type": "STRING"},
            "date": {"type": "STRING"},
            "gst_no": {"type": "STRING"},
            "gst_percentage": {"type": "STRING"},
            "net_amount": {"type": "STRING"},
            "cgst_amount": {"type": "STRING"},
            "sgst_amount": {"type": "STRING"},
            "igst_amount": {"type": "STRING"},
            "total_gst_amount": {"type": "STRING"},
            "grand_total": {"type": "STRING"},
            "type": {
                "type": "STRING",
                "enum": ["Computer Generated", "Manual"]
            }
        },
        "required": [
            "company_name", "invoice_no", "date", "gst_no", "net_amount",
            "total_gst_amount", "grand_total", "type"
        ]
    }

    models_to_try = get_available_flash_models(client)
    response = None
    last_error = None

    for model_name in models_to_try:
        for attempt in range(3):
            try:
                response = client.models.generate_content(
                    model=model_name,
                    contents=[uploaded_ref, prompt],
                    config=types.GenerateContentConfig(
                        response_mime_type="application/json",
                        response_schema=invoice_schema,
                        temperature=0.0
                    )
                )
                if response and response.text:
                    break
            except Exception as err:
                last_error = err
                err_str = str(err)
                if any(code in err_str for code in ["503", "429", "UNAVAILABLE", "RESOURCE_EXHAUSTED"]):
                    time.sleep(2 * (attempt + 1))
                    continue
                else:
                    break
        if response and response.text:
            break

    try:
        client.files.delete(name=uploaded_ref.name)
    except Exception:
        pass

    if not response or not response.text:
        raise Exception(f"AI service temporarily unavailable: {last_error}")

    try:
        data = json.loads(response.text.strip())
        for key in ["net_amount", "cgst_amount", "sgst_amount", "igst_amount", "total_gst_amount", "grand_total"]:
            data[key] = f"{clean_float(data.get(key, 0.0)):.2f}"

        if data.get("type") not in ["Computer Generated", "Manual"]:
            data["type"] = "Manual"

        return data
    except Exception:
        return {
            "company_name": "Not found",
            "invoice_no": "Not found",
            "date": "Not found",
            "gst_no": "Not found",
            "gst_percentage": "18",
            "net_amount": "0.00",
            "cgst_amount": "0.00",
            "sgst_amount": "0.00",
            "igst_amount": "0.00",
            "total_gst_amount": "0.00",
            "grand_total": "0.00",
            "type": "Manual"
        }

# --- PAGE NAVIGATION ROUTES ---

@app.route("/")
def dashboard_view():
    return render_template("dashboard.html")

@app.route("/upload")
def upload_view():
    return render_template("upload.html")

@app.route("/export")
def export_view():
    return render_template("export.html")

@app.route("/healthz")
def health_check():
    return jsonify({"status": "healthy"}), 200

# --- DATA AND ACTION ROUTES ---

@app.route("/api/dashboard-metrics")
def get_dashboard_metrics():
    start_date_str = request.args.get("start_date", "").strip()
    end_date_str = request.args.get("end_date", "").strip()
    inv_type = request.args.get("type", "all").strip()
    tax_cat = request.args.get("tax_cat", "all").strip()

    start_d = parse_date_to_comparable(start_date_str) if start_date_str else None
    end_d = parse_date_to_comparable(end_date_str) if end_date_str else None

    conn = None
    try:
        conn = get_db_connection()
        with conn.cursor() as cursor:
            # Check if created_at column exists in invoice_items table
            cursor.execute("""
                SELECT COUNT(*) as has_col 
                FROM information_schema.COLUMNS 
                WHERE TABLE_SCHEMA = DATABASE() 
                  AND TABLE_NAME = 'invoice_items' 
                  AND COLUMN_NAME = 'created_at'
            """)
            has_created_at = cursor.fetchone().get("has_col", 0) > 0

            date_col = "created_at" if has_created_at else "invoice_date AS created_at"

            query = f"""
                SELECT id, batch_id, invoice_type, company_name, invoice_no, invoice_date, gst_no,
                       net_amount, cgst_amount, sgst_amount, igst_amount, total_gst_amount, grand_total, {date_col}
                FROM invoice_items
                WHERE 1=1
            """
            params = []

            if inv_type and inv_type != "all":
                query += " AND invoice_type = %s"
                params.append(inv_type)

            if tax_cat == "cgst_sgst":
                query += " AND (cgst_amount > 0 OR sgst_amount > 0)"
            elif tax_cat == "igst":
                query += " AND igst_amount > 0"

            query += " ORDER BY id DESC"
            cursor.execute(query, tuple(params))
            all_rows = cursor.fetchall()

        # Date normalization & filtering in Python (handles DD/MM/YYYY, YYYY-MM-DD, created_at)
        filtered_rows = []
        for r in all_rows:
            rec_date = parse_date_to_comparable(r.get("created_at")) or parse_date_to_comparable(r.get("invoice_date"))
            if start_d and rec_date and rec_date < start_d:
                continue
            if end_d and rec_date and rec_date > end_d:
                continue
            filtered_rows.append(r)

        # Aggregate metrics from the correctly filtered rows
        tot_count = len(filtered_rows)
        manual_count = sum(1 for r in filtered_rows if r.get("invoice_type") == "Manual")
        comp_count = sum(1 for r in filtered_rows if r.get("invoice_type") == "Computer Generated")
        net_tot = sum(clean_float(r.get("net_amount")) for r in filtered_rows)
        cgst_tot = sum(clean_float(r.get("cgst_amount")) for r in filtered_rows)
        sgst_tot = sum(clean_float(r.get("sgst_amount")) for r in filtered_rows)
        igst_tot = sum(clean_float(r.get("igst_amount")) for r in filtered_rows)
        gst_tot = sum(clean_float(r.get("total_gst_amount")) for r in filtered_rows)
        grand_tot = sum(clean_float(r.get("grand_total")) for r in filtered_rows)

        totals = {
            "total_invoices": tot_count,
            "manual_count": manual_count,
            "computer_count": comp_count,
            "total_net": net_tot,
            "total_cgst": cgst_tot,
            "total_sgst": sgst_tot,
            "total_igst": igst_tot,
            "total_gst": gst_tot,
            "grand_total": grand_tot
        }

        # Daily timeline aggregates
        day_map = {}
        for r in filtered_rows:
            d_obj = parse_date_to_comparable(r.get("created_at")) or parse_date_to_comparable(r.get("invoice_date"))
            d_label = d_obj.strftime("%Y-%m-%d") if d_obj else "General"
            day_map[d_label] = day_map.get(d_label, 0.0) + clean_float(r.get("grand_total"))

        sorted_days = sorted(day_map.keys())[-7:]
        trends = [{"d_date": d, "day_total": day_map[d]} for d in sorted_days]

        # Recent records for table preview (top 15)
        recent = [clean_row(r) for r in filtered_rows[:15]]

        return jsonify({
            "success": True,
            "totals": totals,
            "types": {"Computer Generated": comp_count, "Manual": manual_count},
            "trends": trends,
            "recent": recent
        })
    except Exception as e:
        print(f"Metrics Error: {e}")
        return jsonify({
            "success": False,
            "error": str(e),
            "totals": {
                "total_invoices": 0, "manual_count": 0, "computer_count": 0,
                "total_net": 0, "total_cgst": 0, "total_sgst": 0,
                "total_igst": 0, "total_gst": 0, "grand_total": 0
            },
            "types": {"Computer Generated": 0, "Manual": 0},
            "trends": [],
            "recent": []
        }), 200
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass

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
                    batch_total_gst, batch_grand_total
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            """
            cursor.execute(master_sql, (
                batch_name, staff_name, len(records), b_net,
                b_cgst, b_sgst, b_igst, b_gst, b_grand
            ))
            batch_id = cursor.lastrowid

            detail_sql = """
                INSERT INTO invoice_items (
                    batch_id, filename, company_name, invoice_no, invoice_date, gst_no,
                    gst_percentage, net_amount, cgst_amount, sgst_amount, igst_amount,
                    total_gst_amount, grand_total, invoice_type
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """
            detail_rows = [
                (
                    batch_id,
                    r.get("filename", ""),
                    r.get("company_name", "Not found"),
                    r.get("invoice_no", "Not found"),
                    r.get("date", "Not found"),
                    r.get("gst_no", "Not found"),
                    r.get("gst_percentage", "18"),
                    clean_float(r.get("net_amount")),
                    clean_float(r.get("cgst_amount")),
                    clean_float(r.get("sgst_amount")),
                    clean_float(r.get("igst_amount")),
                    clean_float(r.get("total_gst_amount")),
                    clean_float(r.get("grand_total")),
                    r.get("type", "Manual")
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

@app.route("/api/export-preview")
def export_preview():
    start_date_str = request.args.get("start_date", "").strip()
    end_date_str = request.args.get("end_date", "").strip()
    inv_type = request.args.get("type", "all").strip()

    start_d = parse_date_to_comparable(start_date_str) if start_date_str else None
    end_d = parse_date_to_comparable(end_date_str) if end_date_str else None

    conn = None
    try:
        conn = get_db_connection()
        with conn.cursor() as cursor:
            cursor.execute("""
                SELECT COUNT(*) as has_col 
                FROM information_schema.COLUMNS 
                WHERE TABLE_SCHEMA = DATABASE() 
                  AND TABLE_NAME = 'invoice_items' 
                  AND COLUMN_NAME = 'created_at'
            """)
            has_created_at = cursor.fetchone().get("has_col", 0) > 0

            date_col = "created_at" if has_created_at else "invoice_date AS created_at"

            query = f"""
                SELECT id, batch_id, invoice_type, company_name, invoice_no, invoice_date, gst_no, 
                       net_amount, cgst_amount, sgst_amount, igst_amount, total_gst_amount, grand_total, {date_col} 
                FROM invoice_items 
                WHERE 1=1
            """
            params = []

            if inv_type and inv_type != "all":
                query += " AND invoice_type = %s"
                params.append(inv_type)

            query += " ORDER BY id DESC"
            cursor.execute(query, tuple(params))
            all_rows = cursor.fetchall()

        filtered_rows = []
        for r in all_rows:
            rec_date = parse_date_to_comparable(r.get("created_at")) or parse_date_to_comparable(r.get("invoice_date"))
            if start_d and rec_date and rec_date < start_d:
                continue
            if end_d and rec_date and rec_date > end_d:
                continue
            filtered_rows.append(clean_row(r))

        return jsonify({"success": True, "rows": filtered_rows[:500]})
    except Exception as e:
        print(f"Export Error: {e}")
        return jsonify({"success": False, "error": str(e), "rows": []}), 200
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass

@app.route("/export-csv")
def export_csv():
    start_date_str = request.args.get("start_date", "").strip()
    end_date_str = request.args.get("end_date", "").strip()
    inv_type = request.args.get("type", "all").strip()

    start_d = parse_date_to_comparable(start_date_str) if start_date_str else None
    end_d = parse_date_to_comparable(end_date_str) if end_date_str else None

    conn = None
    try:
        conn = get_db_connection()
        with conn.cursor() as cursor:
            cursor.execute("""
                SELECT COUNT(*) as has_col 
                FROM information_schema.COLUMNS 
                WHERE TABLE_SCHEMA = DATABASE() 
                  AND TABLE_NAME = 'invoice_items' 
                  AND COLUMN_NAME = 'created_at'
            """)
            has_created_at = cursor.fetchone().get("has_col", 0) > 0

            date_col = "created_at" if has_created_at else "invoice_date AS created_at"

            query = f"""
                SELECT id, batch_id, invoice_type, company_name, invoice_no, invoice_date, gst_no,
                       net_amount, cgst_amount, sgst_amount, igst_amount, total_gst_amount, grand_total, {date_col}
                FROM invoice_items WHERE 1=1
            """
            params = []

            if inv_type and inv_type != "all":
                query += " AND invoice_type = %s"
                params.append(inv_type)

            query += " ORDER BY id ASC"
            cursor.execute(query, tuple(params))
            all_records = cursor.fetchall()

        filtered_records = []
        for r in all_records:
            rec_date = parse_date_to_comparable(r.get("created_at")) or parse_date_to_comparable(r.get("invoice_date"))
            if start_d and rec_date and rec_date < start_d:
                continue
            if end_d and rec_date and rec_date > end_d:
                continue
            filtered_records.append(r)

        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow([
            "Item ID", "Batch ID", "Invoice Type", "Company Name", "Invoice No", 
            "Invoice Date", "GST No", "Net Amount", "CGST", "SGST", "IGST", 
            "Total GST", "Grand Total", "Date"
        ])

        for r in filtered_records:
            writer.writerow([
                r["id"], r["batch_id"], r["invoice_type"], r["company_name"], r["invoice_no"],
                r["invoice_date"], r["gst_no"], r["net_amount"], r["cgst_amount"],
                r["sgst_amount"], r["igst_amount"], r["total_gst_amount"], r["grand_total"],
                r.get("created_at") or r.get("invoice_date") or ""
            ])

        output.seek(0)
        filename = f"Invoices_Export_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
        return Response(
            output.getvalue(),
            mimetype="text/csv",
            headers={"Content-Disposition": f"attachment; filename={filename}"}
        )
    except Exception as e:
        return f"Export failed: {str(e)}", 500
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
