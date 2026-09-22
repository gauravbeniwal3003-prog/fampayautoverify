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
from fastapi.responses import Response, HTMLResponse
from pydantic import BaseModel

# ============================================================
# CONFIGURATION
# ============================================================
GMAIL_USER = os.getenv("GMAIL_USER")
GMAIL_APP_PASSWORD = os.getenv("GMAIL_APP_PASSWORD")
UPI_ID = os.getenv("UPI_ID", "beniwalgaurav@fam")
PAYEE_NAME = os.getenv("PAYEE_NAME", "Gaurav Beniwal")
DB_PATH = os.getenv("DB_PATH", "./payments.db")
SEARCH_DAYS = 3

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
# EMAIL VERIFIER — searches last 3 days, reports only
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

    def _parse_payment(self, body):
        amount = None
        for pattern in [
            r'(?:Rs\.?|INR|₹)\s*([\d,]+\.?\d*)',
            r'([\d,]+\.?\d*)\s*(?:Rs\.?|INR|₹)',
        ]:
            m = re.search(pattern, body, re.IGNORECASE)
            if m:
                amount = m.group(1).replace(",", "")
                break

        utr_match = re.search(r'\b(\d{12})\b', body)
        utr = utr_match.group(1) if utr_match else None

        name_match = re.search(r'(?:from|by|paid by)[:\s]+([A-Za-z\s]{2,40})', body, re.IGNORECASE)
        sender_name = name_match.group(1).strip() if name_match else "Unknown"

        note_match = re.search(r'(?:note|ref(?:erence)?|remark)[:\s]+([A-Za-z0-9_\-]+)', body, re.IGNORECASE)
        note = note_match.group(1) if note_match else None

        return {"amount": amount, "utr": utr, "sender_name": sender_name, "note": note}

    def fetch_all_payments(self, days=SEARCH_DAYS):
        try:
            mail = self._connect()
        except Exception as e:
            return {"error": f"IMAP connection failed: {str(e)}", "payments": []}

        payments = []
        try:
            since_date = (datetime.now() - timedelta(days=days)).strftime("%d-%b-%Y")
            status, messages = mail.search(None, f'(SINCE "{since_date}")')
            if status != "OK":
                mail.logout()
                return {"error": "Email search failed", "payments": []}

            email_ids = messages[0].split()
            for eid in reversed(email_ids):
                status, msg_data = mail.fetch(eid, "(RFC822)")
                if status != "OK":
                    continue

                msg = email.message_from_bytes(msg_data[0][1])
                sender = (msg.get("From") or "").lower()
                subject = (msg.get("Subject") or "").lower()

                if not ("fam" in sender or "fam" in subject or "payment" in subject or "upi" in subject):
                    continue

                body = self._extract_body(msg)
                if not body:
                    continue

                parsed = self._parse_payment(body)
                if not parsed["utr"] and not parsed["amount"]:
                    continue

                parsed["subject"] = msg.get("Subject", "")
                parsed["received_at"] = msg.get("Date", "")
                payments.append(parsed)

            mail.logout()
            return {"error": None, "payments": payments}
        except Exception as e:
            try:
                mail.logout()
            except Exception:
                pass
            return {"error": f"Verification error: {str(e)}", "payments": []}

    def find_by_utr(self, utr, days=SEARCH_DAYS):
        result = self.fetch_all_payments(days=days)
        if result.get("error"):
            return {"found": False, "message": result["error"]}
        for p in result["payments"]:
            if p.get("utr") == utr:
                return {"found": True, "payment": p}
        return {"found": False, "message": f"UTR {utr} not found in last {days} days"}

    def find_by_amount(self, expected_amount, days=SEARCH_DAYS):
        result = self.fetch_all_payments(days=days)
        if result.get("error"):
            return {"found": False, "message": result["error"], "matches": []}
        matches = []
        for p in result["payments"]:
            if not p.get("amount"):
                continue
            try:
                if abs(float(p["amount"]) - float(expected_amount)) <= 0.01:
                    matches.append(p)
            except ValueError:
                continue
        if matches:
            return {"found": True, "matches": matches}
        return {"found": False, "matches": [], "message": f"No payment of {expected_amount} found in last {days} days"}

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

# -------- DOCS PAGE --------
@app.get("/", response_class=HTMLResponse)
async def home():
    return """
    <html><head><title>FamPay UPI Verify</title>
    <style>
    body{font-family:system-ui;max-width:960px;margin:40px auto;padding:20px;background:#fafafa;color:#222}
    h1{color:#111}
    a{color:#0066cc;text-decoration:none;word-break:break-all}
    a:hover{text-decoration:underline}
    .card{background:white;padding:20px;border-radius:8px;margin:15px 0;box-shadow:0 2px 6px rgba(0,0,0,0.06)}
    .endpoint{display:block;margin:8px 0;padding:10px 14px;background:#f4f8ff;border-left:3px solid #0066cc;border-radius:4px;font-size:14px}
    h2{font-size:18px;margin-top:0}
    .hint{color:#555;font-size:14px;margin:6px 0}
    </style></head><body>
    <h1>FamPay UPI Verification System</h1>
    <p class="hint">Gateway reports info only. Your website decides the rules.</p>

    <div class="card"><h2>1. Health Check</h2>
    <a class="endpoint" href="/health">https://fampayautoverify.onrender.com/health</a></div>

    <div class="card"><h2>2. Create Order — QR Image</h2>
    <a class="endpoint" href="/create-order-qr?amount=10&note=ORDER1001">https://fampayautoverify.onrender.com/create-order-qr?amount=10&note=ORDER1001</a></div>

    <div class="card"><h2>3. Create Order — JSON</h2>
    <a class="endpoint" href="/create-order-get?amount=10&note=ORDER1001">https://fampayautoverify.onrender.com/create-order-get?amount=10&note=ORDER1001</a></div>

    <div class="card"><h2>4. Check Order Status</h2>
    <a class="endpoint" href="/order/ORD20260922162131U1T1790094090">https://fampayautoverify.onrender.com/order/ORD20260922162131U1T1790094090</a></div>

    <div class="card"><h2>5. Verify by UTR Only</h2>
    <a class="endpoint" href="/verify-utr-only?utr=005228066783">https://fampayautoverify.onrender.com/verify-utr-only?utr=005228066783</a>
    <p class="hint">Returns payment details if found in last 3 days. Never blocks.</p></div>

    <div class="card"><h2>6. Verify by Order ID + UTR</h2>
    <a class="endpoint" href="/verify-by-utr-get?order_id=ORD20260922162131U1T1790094090&utr=005228066783">https://fampayautoverify.onrender.com/verify-by-utr-get?order_id=ORD20260922162131U1T1790094090&utr=005228066783</a>
    <p class="hint">Returns order + payment details. Never blocks.</p></div>

    <div class="card"><h2>7. Verify by Order ID Only</h2>
    <a class="endpoint" href="/verify-by-amount-get?order_id=ORD20260922162131U1T1790094090">https://fampayautoverify.onrender.com/verify-by-amount-get?order_id=ORD20260922162131U1T1790094090</a></div>

    <div class="card"><h2>8. Verify by Amount Only</h2>
    <a class="endpoint" href="/verify-amount-only?amount=1">https://fampayautoverify.onrender.com/verify-amount-only?amount=1</a>
    <p class="hint">Returns all matching payments from last 3 days. Never blocks.</p></div>

    <div class="card"><h2>9. All Payments — Last 3 Days</h2>
    <a class="endpoint" href="/payments?days=3">https://fampayautoverify.onrender.com/payments?days=3</a></div>

    <div class="card"><h2>10. All Orders Stored</h2>
    <a class="endpoint" href="/orders">https://fampayautoverify.onrender.com/orders</a></div>

    <div class="card"><h2>11. Swagger UI</h2>
    <a class="endpoint" href="/docs">https://fampayautoverify.onrender.com/docs</a></div>

    <div class="card"><h2>12. ReDoc</h2>
    <a class="endpoint" href="/redoc">https://fampayautoverify.onrender.com/redoc</a></div>
    </body></html>
    """

@app.get("/health")
async def health():
    return {"status": "ok", "service": "fam-pay-verifier"}

# -------- CREATE ORDER --------
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

@app.get("/create-order-get")
async def create_order_get(amount: float, note: str):
    return await create_order(CreateOrderRequest(amount=amount, note=note))

@app.get("/create-order-qr")
async def create_order_qr(amount: float, note: str):
    order_id = _make_order_id(note)
    upi_url = _make_upi_url(amount, note)
    qr_bytes = _make_qr_png(upi_url)
    _store_order(order_id, amount, note)
    return Response(content=qr_bytes, media_type="image/png")

# -------- VERIFY BY UTR ONLY — pure lookup, no blocking --------
@app.get("/verify-utr-only")
async def verify_utr_only(utr: str):
    """
    Look up a UTR in last 3 days of FamPay emails.
    Always returns details if found. Never blocks on 'already used'.
    """
    result = verifier.find_by_utr(utr, days=SEARCH_DAYS)
    if result.get("found"):
        p = result["payment"]
        return {
            "found": True,
            "utr": utr,
            "amount": p.get("amount"),
            "sender_name": p.get("sender_name"),
            "note": p.get("note"),
            "received_at": p.get("received_at"),
            "subject": p.get("subject"),
        }
    return {"found": False, "utr": utr, "message": result.get("message")}

# -------- VERIFY BY AMOUNT ONLY — pure lookup, no blocking --------
@app.get("/verify-amount-only")
async def verify_amount_only(amount: float):
    """
    Look up all payments matching an amount in last 3 days.
    Always returns all matches. Never blocks.
    """
    result = verifier.find_by_amount(amount, days=SEARCH_DAYS)
    if result.get("found"):
        return {
            "found": True,
            "amount": amount,
            "match_count": len(result["matches"]),
            "matches": result["matches"],
        }
    return {"found": False, "amount": amount, "match_count": 0, "matches": [], "message": result.get("message")}

# -------- PAYMENTS LIST --------
@app.get("/payments")
async def payments(days: int = 3):
    days = min(max(days, 1), SEARCH_DAYS)
    result = verifier.fetch_all_payments(days=days)
    if result.get("error"):
        return {"error": result["error"], "payments": []}
    return {
        "days_searched": days,
        "count": len(result["payments"]),
        "payments": result["payments"],
    }

# -------- VERIFY BY UTR (with order) — pure lookup, no blocking --------
@app.post("/verify-by-utr")
async def verify_by_utr(req: VerifyRequest):
    """
    Return payment details for a UTR, plus the stored order if present.
    Never blocks on 'already used' — that decision belongs to the website.
    """
    order_info = None
    with get_db() as conn:
        order = conn.execute("SELECT * FROM orders WHERE order_id = ?", (req.order_id,)).fetchone()
        if order:
            order_info = dict(order)

    utr_result = verifier.find_by_utr(req.utr, days=SEARCH_DAYS)
    if utr_result.get("found"):
        p = utr_result["payment"]
        return {
            "found": True,
            "utr": req.utr,
            "amount": p.get("amount"),
            "sender_name": p.get("sender_name"),
            "note_in_email": p.get("note"),
            "received_at": p.get("received_at"),
            "order": order_info,
        }
    return {
        "found": False,
        "utr": req.utr,
        "order": order_info,
        "message": utr_result.get("message"),
    }

@app.get("/verify-by-utr-get")
async def verify_by_utr_get(order_id: str, utr: str):
    return await verify_by_utr(VerifyRequest(order_id=order_id, utr=utr))

# -------- VERIFY BY AMOUNT (with order) — pure lookup, no blocking --------
@app.post("/verify-by-amount")
async def verify_by_amount(req: VerifyByAmountRequest):
    """
    Look up payments matching the stored order's amount.
    Never blocks. Website handles rules.
    """
    order_info = None
    with get_db() as conn:
        order = conn.execute("SELECT * FROM orders WHERE order_id = ?", (req.order_id,)).fetchone()
        if order:
            order_info = dict(order)

    if not order_info:
        return {"found": False, "order_id": req.order_id, "message": "Order not found", "matches": []}

    result = verifier.find_by_amount(order_info["amount"], days=SEARCH_DAYS)
    if result.get("found"):
        return {
            "found": True,
            "order": order_info,
            "match_count": len(result["matches"]),
            "matches": result["matches"],
        }
    return {
        "found": False,
        "order": order_info,
        "match_count": 0,
        "matches": [],
        "message": result.get("message"),
    }

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

# -------- LIST ALL ORDERS --------
@app.get("/orders")
async def list_orders():
    with get_db() as conn:
        rows = conn.execute("SELECT * FROM orders ORDER BY created_at DESC LIMIT 200").fetchall()
        return {"count": len(rows), "orders": [dict(r) for r in rows]}
