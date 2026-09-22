import imaplib
import email
import re
import os
import sqlite3
from datetime import datetime, timedelta
from contextlib import contextmanager
from io import BytesIO
import base64

import qrcode
from fastapi import FastAPI, HTTPException
from fastapi.responses import Response, HTMLResponse, JSONResponse
from pydantic import BaseModel

# ============================================================
# CONFIGURATION
# ============================================================
GMAIL_USER = os.getenv("GMAIL_USER", "beniwalgaurav@fam")                  # MUST be set on Render
GMAIL_APP_PASSWORD = os.getenv("GMAIL_APP_PASSWORD", "qkvjehdeidsrishw")  # MUST be set on Render
UPI_ID = os.getenv("UPI_ID", "beniwalgaurav@fam")
PAYEE_NAME = os.getenv("PAYEE_NAME", "Gaurav Beniwal")
DB_PATH = os.getenv("DB_PATH", "./payments.db")

if not GMAIL_USER or not GMAIL_APP_PASSWORD:
    raise RuntimeError("Missing GMAIL_USER or GMAIL_APP_PASSWORD env vars")

# ============================================================
# DATABASE
# ============================================================
def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS orders (
            order_id TEXT PRIMARY KEY,
            amount REAL NOT NULL,
            note TEXT NOT NULL,
            status TEXT DEFAULT 'PENDING',
            utr TEXT,
            sender_name TEXT,
            created_at TEXT NOT NULL,
            verified_at TEXT
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS used_utrs (
            utr TEXT PRIMARY KEY,
            order_id TEXT,
            created_at TEXT NOT NULL
        )
    """)
    conn.commit()
    conn.close()

init_db()

@contextmanager
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()

# ============================================================
# EMAIL VERIFIER
# ============================================================
class FamPayEmailVerifier:
    def __init__(self, gmail_user, app_password):
        self.gmail_user = gmail_user
        self.app_password = app_password

    def _connect(self):
        mail = imaplib.IMAP4_SSL("imap.gmail.com", 993)
        mail.login(self.gmail_user, self.app_password)
        mail.select("inbox")
        return mail

    def _extract_body(self, msg):
        if msg.is_multipart():
            for part in msg.walk():
                ct = part.get_content_type()
                if ct == "text/plain":
                    try:
                        return part.get_payload(decode=True).decode("utf-8", errors="ignore")
                    except Exception:
                        continue
                elif ct == "text/html":
                    try:
                        html = part.get_payload(decode=True).decode("utf-8", errors="ignore")
                        return re.sub(r"<[^>]+>", " ", html)
                    except Exception:
                        continue
        else:
            try:
                return msg.get_payload(decode=True).decode("utf-8", errors="ignore")
            except Exception:
                return None
        return None

    def find_payment(self, expected_amount, note, max_age_minutes=60):
        try:
            mail = self._connect()
        except Exception as e:
            return {"verified": False, "message": f"IMAP connection failed: {str(e)}"}

        try:
            since_date = (datetime.now() - timedelta(minutes=max_age_minutes)).strftime("%d-%b-%Y")
            status, messages = mail.search(None, f'(SINCE "{since_date}")')
            if status != "OK":
                mail.logout()
                return {"verified": False, "message": "Email search failed"}

            email_ids = messages[0].split()
            for eid in reversed(email_ids):
                status, msg_data = mail.fetch(eid, "(RFC822)")
                if status != "OK":
                    continue

                msg = email.message_from_bytes(msg_data[0][1])
                sender = (msg.get("From") or "").lower()
                subject = (msg.get("Subject") or "").lower()

                if not ("fam" in sender or "fam" in subject or "payment" in subject):
                    continue

                body = self._extract_body(msg)
                if not body:
                    continue

                amount_patterns = [
                    r'(?:Rs\.?|INR|₹)\s*([\d,]+\.?\d*)',
                    r'([\d,]+\.?\d*)\s*(?:Rs\.?|INR|₹)',
                ]
                found_amount = None
                for pattern in amount_patterns:
                    m = re.search(pattern, body, re.IGNORECASE)
                    if m:
                        found_amount = m.group(1).replace(",", "")
                        break
                if not found_amount:
                    continue

                try:
                    if abs(float(found_amount) - float(expected_amount)) > 0.01:
                        continue
                except ValueError:
                    continue

                utr_match = re.search(r'\b(\d{12})\b', body)
                utr = utr_match.group(1) if utr_match else None

                note_found = note.lower() in body.lower() if note else True
                if not note_found:
                    continue

                name_match = re.search(r'(?:from|by|paid by)[:\s]+([A-Za-z\s]+)', body, re.IGNORECASE)
                sender_name = name_match.group(1).strip() if name_match else "Unknown"

                mail.logout()
                return {
                    "verified": True,
                    "amount": found_amount,
                    "utr": utr,
                    "sender_name": sender_name,
                    "note_matched": note_found,
                }

            mail.logout()
            return {"verified": False, "message": "No matching payment found in recent emails"}
        except Exception as e:
            try:
                mail.logout()
            except Exception:
                pass
            return {"verified": False, "message": f"Verification error: {str(e)}"}

# ============================================================
# FASTAPI APP
# ============================================================
app = FastAPI(title="FamPay UPI Verification System")
verifier = FamPayEmailVerifier(GMAIL_USER, GMAIL_APP_PASSWORD)

class CreateOrderRequest(BaseModel):
    amount: float
    note: str

class VerifyRequest(BaseModel):
    order_id: str
    utr: str

class VerifyByAmountRequest(BaseModel):
    order_id: str

def _make_upi_url(amount, note):
    return f"upi://pay?pa={UPI_ID}&pn={PAYEE_NAME}&am={amount:.2f}&cu=INR&tn={note}"

def _make_order_id(note):
    return f"ORD{datetime.now().strftime('%Y%m%d%H%M%S')}{note}"

def _make_qr_png(upi_url):
    qr = qrcode.QRCode(version=1, box_size=10, border=4)
    qr.add_data(upi_url)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    buf = BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()

def _store_order(order_id, amount, note):
    with get_db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO orders (order_id, amount, note, status, created_at) VALUES (?, ?, ?, 'PENDING', ?)",
            (order_id, amount, note, datetime.now().isoformat())
        )
        conn.commit()

# -------- HOME / DOCS PAGE --------
@app.get("/", response_class=HTMLResponse)
async def home():
    return """
    <html><head><title>FamPay UPI Verify</title>
    <style>
    body{font-family:system-ui;max-width:900px;margin:40px auto;padding:20px;background:#fafafa;color:#222}
    h1{color:#111} a{color:#0066cc;text-decoration:none} a:hover{text-decoration:underline}
    .card{background:white;padding:20px;border-radius:8px;margin:15px 0;box-shadow:0 2px 6px rgba(0,0,0,0.06)}
    code{background:#f0f0f0;padding:2px 6px;border-radius:4px;font-size:14px}
    .endpoint{display:block;margin:8px 0;padding:8px 12px;background:#f8f8f8;border-left:3px solid #0066cc;border-radius:4px}
    </style></head><body>
    <h1>FamPay UPI Verification System</h1>
    <p>Personal payment gateway with QR generation, email verification, and UTR check.</p>

    <div class="card"><h2>1. Health Check</h2>
    <a class="endpoint" href="/health">GET /health</a></div>

    <div class="card"><h2>2. Create Order (JSON)</h2>
    <a class="endpoint" href="/create-order-get?amount=10&note=TEST001">/create-order-get?amount=10&note=TEST001</a>
    <p>Returns full order JSON including base64 QR.</p></div>

    <div class="card"><h2>3. Create Order (QR Image)</h2>
    <a class="endpoint" href="/create-order-qr?amount=10&note=TEST001">/create-order-qr?amount=10&note=TEST001</a>
    <p>Renders the QR image directly in browser. Scan with UPI app.</p></div>

    <div class="card"><h2>4. Check Order Status</h2>
    <a class="endpoint" href="/order/ORD20250922143012TEST001">/order/{order_id}</a></div>

    <div class="card"><h2>5. Verify by UTR</h2>
    <a class="endpoint" href="/verify-by-utr-get?order_id=ORD20250922143012TEST001&utr=123456789012">/verify-by-utr-get?order_id=...&utr=...</a></div>

    <div class="card"><h2>6. Verify by Amount</h2>
    <a class="endpoint" href="/verify-by-amount-get?order_id=ORD20250922143012TEST001">/verify-by-amount-get?order_id=...</a></div>

    <div class="card"><h2>7. Interactive Docs</h2>
    <a class="endpoint" href="/docs">/docs</a>
    <a class="endpoint" href="/redoc">/redoc</a></div>
    </body></html>
    """

@app.get("/health")
async def health():
    return {"status": "ok", "service": "fam-pay-verifier"}

# -------- CREATE ORDER (POST) --------
@app.post("/create-order")
async def create_order(req: CreateOrderRequest):
    order_id = _make_order_id(req.note)
    upi_url = _make_upi_url(req.amount, req.note)
    qr_bytes = _make_qr_png(upi_url)
    qr_b64 = base64.b64encode(qr_bytes).decode()
    _store_order(order_id, req.amount, req.note)
    return {
        "order_id": order_id,
        "amount": req.amount,
        "note": req.note,
        "upi_url": upi_url,
        "qr_image_base64": qr_b64,
    }

# -------- CREATE ORDER (GET JSON) --------
@app.get("/create-order-get")
async def create_order_get(amount: float, note: str):
    return await create_order(CreateOrderRequest(amount=amount, note=note))

# -------- CREATE ORDER (GET QR IMAGE) --------
@app.get("/create-order-qr")
async def create_order_qr(amount: float, note: str):
    order_id = _make_order_id(note)
    upi_url = _make_upi_url(amount, note)
    qr_bytes = _make_qr_png(upi_url)
    _store_order(order_id, amount, note)
    return Response(content=qr_bytes, media_type="image/png")

# -------- VERIFY BY UTR (POST) --------
@app.post("/verify-by-utr")
async def verify_by_utr(req: VerifyRequest):
    with get_db() as conn:
        order = conn.execute("SELECT * FROM orders WHERE order_id = ?", (req.order_id,)).fetchone()
        if not order:
            raise HTTPException(status_code=404, detail="Order not found")
        if order["status"] == "VERIFIED":
            return {
                "verified": True, "status": "VERIFIED", "amount": order["amount"],
                "utr": order["utr"], "sender_name": order["sender_name"], "note": order["note"],
            }
        used = conn.execute("SELECT * FROM used_utrs WHERE utr = ?", (req.utr,)).fetchone()
        if used:
            return {"verified": False, "message": "This UTR has already been used for another order"}

    result = verifier.find_payment(order["amount"], order["note"], max_age_minutes=60)

    if result.get("verified"):
        email_utr = result.get("utr", "")
        if email_utr and req.utr and email_utr != req.utr:
            return {"verified": False, "message": f"UTR mismatch: email shows {email_utr}, you submitted {req.utr}"}

        with get_db() as conn:
            conn.execute(
                "UPDATE orders SET status='VERIFIED', utr=?, sender_name=?, verified_at=? WHERE order_id=?",
                (req.utr, result.get("sender_name", ""), datetime.now().isoformat(), req.order_id)
            )
            conn.execute(
                "INSERT OR IGNORE INTO used_utrs (utr, order_id, created_at) VALUES (?, ?, ?)",
                (req.utr, req.order_id, datetime.now().isoformat())
            )
            conn.commit()
        return {
            "verified": True, "status": "VERIFIED", "amount": result.get("amount"),
            "utr": req.utr, "sender_name": result.get("sender_name"),
        }
    return {"verified": False, "message": result.get("message", "Payment not found. Retry in 30-60 seconds.")}

# -------- VERIFY BY UTR (GET) --------
@app.get("/verify-by-utr-get")
async def verify_by_utr_get(order_id: str, utr: str):
    return await verify_by_utr(VerifyRequest(order_id=order_id, utr=utr))

# -------- VERIFY BY AMOUNT (POST) --------
@app.post("/verify-by-amount")
async def verify_by_amount(req: VerifyByAmountRequest):
    with get_db() as conn:
        order = conn.execute("SELECT * FROM orders WHERE order_id = ?", (req.order_id,)).fetchone()
        if not order:
            raise HTTPException(status_code=404, detail="Order not found")
        if order["status"] == "VERIFIED":
            return {
                "verified": True, "status": "VERIFIED", "amount": order["amount"],
                "utr": order["utr"], "sender_name": order["sender_name"],
            }

    result = verifier.find_payment(order["amount"], order["note"], max_age_minutes=60)
    if result.get("verified"):
        with get_db() as conn:
            conn.execute(
                "UPDATE orders SET status='VERIFIED', utr=?, sender_name=?, verified_at=? WHERE order_id=?",
                (result.get("utr", ""), result.get("sender_name", ""), datetime.now().isoformat(), req.order_id)
            )
            conn.commit()
        return {
            "verified": True, "status": "VERIFIED", "amount": result.get("amount"),
            "utr": result.get("utr"), "sender_name": result.get("sender_name"),
        }
    return {"verified": False, "message": result.get("message", "Payment not found. Retry in 30-60 seconds.")}

# -------- VERIFY BY AMOUNT (GET) --------
@app.get("/verify-by-amount-get")
async def verify_by_amount_get(order_id: str):
    return await verify_by_amount(VerifyByAmountRequest(order_id=order_id))

# -------- CHECK ORDER --------
@app.get("/order/{order_id}")
async def get_order(order_id: str):
    with get_db() as conn:
        order = conn.execute("SELECT * FROM orders WHERE order_id = ?", (order_id,)).fetchone()
        if not order:
            raise HTTPException(status_code=404, detail="Order not found")
        return dict(order)
