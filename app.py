import imaplib
import email
import re
import os
import sqlite3
import json
from datetime import datetime, timedelta
from contextlib import contextmanager

import qrcode
from io import BytesIO
import base64
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

# ============================================================
# CONFIGURATION — Set these as Environment Variables on Render
# ============================================================
GMAIL_USER = os.getenv("GMAIL_USER", "beniwalgaurav@fam")                  # MUST be set on Render
GMAIL_APP_PASSWORD = os.getenv("GMAIL_APP_PASSWORD", "qkvjehdeidsrishw")  # MUST be set on Render
UPI_ID = os.getenv("UPI_ID", "beniwalgaurav@fam")
PAYEE_NAME = os.getenv("PAYEE_NAME", "Gaurav Beniwal")
DB_PATH = os.getenv("DB_PATH", "/var/data/payments.db")

# ============================================================
# DATABASE — Persistent storage for orders and verified payments
# ============================================================
def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
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
# EMAIL VERIFIER — Reads FamPay notification emails
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
        """Extract plain text body from email message."""
        if msg.is_multipart():
            for part in msg.walk():
                content_type = part.get_content_type()
                if content_type == "text/plain":
                    try:
                        return part.get_payload(decode=True).decode("utf-8", errors="ignore")
                    except Exception:
                        continue
                elif content_type == "text/html":
                    try:
                        html = part.get_payload(decode=True).decode("utf-8", errors="ignore")
                        # Strip HTML tags roughly
                        return re.sub(r"<[^>]+>", " ", html)
                    except Exception:
                        continue
        else:
            try:
                return msg.get_payload(decode=True).decode("utf-8", errors="ignore")
            except Exception:
                return None
        return None

    def find_payment(self, expected_amount, note, max_age_minutes=30):
        """
        Search recent emails for a FamPay payment matching the amount and note.
        Returns dict with verified status, amount, utr, sender.
        """
        try:
            mail = self._connect()
        except Exception as e:
            return {"verified": False, "message": f"IMAP connection failed: {str(e)}"}

        try:
            # Search emails from last 30 minutes (safety margin)
            since_date = (datetime.now() - timedelta(minutes=max_age_minutes)).strftime("%d-%b-%Y")
            # Broad search — FamPay emails typically come from no-reply@fam.co or similar
            status, messages = mail.search(None, f'(SINCE "{since_date}")')

            if status != "OK":
                mail.logout()
                return {"verified": False, "message": "Email search failed"}

            email_ids = messages[0].split()
            # Process newest first
            for eid in reversed(email_ids):
                status, msg_data = mail.fetch(eid, "(RFC822)")
                if status != "OK":
                    continue

                msg = email.message_from_bytes(msg_data[0][1])
                sender = (msg.get("From") or "").lower()
                subject = (msg.get("Subject") or "").lower()

                # Filter: only look at emails likely from FamPay
                # Adjust this if your FamPay notifications come from a different sender
                if not ("fam" in sender or "fam" in subject or "payment" in subject):
                    continue

                body = self._extract_body(msg)
                if not body:
                    continue

                # Extract amount — look for patterns like "Rs 100", "₹100", "INR 100"
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

                # Check amount match (tolerance 0.01)
                try:
                    if abs(float(found_amount) - float(expected_amount)) > 0.01:
                        continue
                except ValueError:
                    continue

                # Extract UTR — 12-digit number
                utr_match = re.search(r'\b(\d{12})\b', body)
                utr = utr_match.group(1) if utr_match else None

                # Check note match — the note should appear in email body
                # This is a secondary security check
                note_found = note.lower() in body.lower() if note else True

                if not note_found:
                    # Amount matched but note didn't — continue searching
                    # (could be a different payment with same amount)
                    continue

                # Extract sender name if possible
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
# FASTAPI APPLICATION
# ============================================================
app = FastAPI(title="FamPay UPI Verification System")
verifier = FamPayEmailVerifier(GMAIL_USER, GMAIL_APP_PASSWORD)

class CreateOrderRequest(BaseModel):
    amount: float
    note: str  # Unique identifier like "ORDER123"

class VerifyRequest(BaseModel):
    order_id: str
    utr: str

class VerifyByAmountRequest(BaseModel):
    order_id: str

@app.get("/")
async def health():
    """Health check endpoint for Better Stack uptime monitoring."""
    return {"status": "ok", "service": "fam-pay-verifier"}

@app.post("/create-order")
async def create_order(req: CreateOrderRequest):
    """
    Create a new order with unique fixed-amount QR.
    Returns base64-encoded QR image and UPI deep link.
    """
    order_id = f"ORD{datetime.now().strftime('%Y%m%d%H%M%S')}{req.note}"
    
    # Build UPI deep link with unique note
    upi_url = (
        f"upi://pay?pa={UPI_ID}"
        f"&pn={PAYEE_NAME}"
        f"&am={req.amount:.2f}"
        f"&cu=INR"
        f"&tn={req.note}"
    )

    # Generate QR code
    qr = qrcode.QRCode(version=1, box_size=10, border=4)
    qr.add_data(upi_url)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")

    buffer = BytesIO()
    img.save(buffer, format="PNG")
    qr_base64 = base64.b64encode(buffer.getvalue()).decode()

    # Store order
    with get_db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO orders (order_id, amount, note, status, created_at) VALUES (?, ?, ?, 'PENDING', ?)",
            (order_id, req.amount, req.note, datetime.now().isoformat())
        )
        conn.commit()

    return {
        "order_id": order_id,
        "amount": req.amount,
        "note": req.note,
        "upi_url": upi_url,
        "qr_image_base64": qr_base64,
        "instructions": "Scan QR or use UPI link. After payment, submit the 12-digit UTR for verification.",
    }

@app.post("/verify-by-utr")
async def verify_by_utr(req: VerifyRequest):
    """
    Verify a payment using the UTR submitted by the user.
    Checks Gmail for a matching FamPay payment email.
    """
    with get_db() as conn:
        order = conn.execute(
            "SELECT * FROM orders WHERE order_id = ?", (req.order_id,)
        ).fetchone()

        if not order:
            raise HTTPException(status_code=404, detail="Order not found")

        if order["status"] == "VERIFIED":
            # Already verified — return cached result
            return {
                "verified": True,
                "status": "VERIFIED",
                "amount": order["amount"],
                "utr": order["utr"],
                "sender_name": order["sender_name"],
                "note": order["note"],
            }

        # Check if UTR was already used
        used = conn.execute(
            "SELECT * FROM used_utrs WHERE utr = ?", (req.utr,)
        ).fetchone()
        if used:
            return {
                "verified": False,
                "message": "This UTR has already been used for another order",
            }

    # Search email for matching payment
    result = verifier.find_payment(
        expected_amount=order["amount"],
        note=order["note"],
        max_age_minutes=60
    )

    if result.get("verified"):
        # Double-check UTR matches if provided
        email_utr = result.get("utr", "")
        if email_utr and req.utr and email_utr != req.utr:
            return {
                "verified": False,
                "message": f"UTR mismatch: email shows {email_utr}, you submitted {req.utr}",
            }

        with get_db() as conn:
            conn.execute(
                """UPDATE orders SET status='VERIFIED', utr=?, sender_name=?, verified_at=?
                   WHERE order_id=?""",
                (req.utr, result.get("sender_name", ""), datetime.now().isoformat(), req.order_id)
            )
            conn.execute(
                "INSERT OR IGNORE INTO used_utrs (utr, order_id, created_at) VALUES (?, ?, ?)",
                (req.utr, req.order_id, datetime.now().isoformat())
            )
            conn.commit()

        return {
            "verified": True,
            "status": "VERIFIED",
            "amount": result.get("amount"),
            "utr": req.utr,
            "sender_name": result.get("sender_name"),
            "note_matched": result.get("note_matched", False),
        }
    else:
        return {
            "verified": False,
            "message": result.get("message", "Payment not found. Email may be delayed — retry in 30-60 seconds."),
        }

@app.post("/verify-by-amount")
async def verify_by_amount(req: VerifyByAmountRequest):
    """
    Fallback: verify by amount + note only (no UTR needed).
    Useful if user can't find UTR.
    """
    with get_db() as conn:
        order = conn.execute(
            "SELECT * FROM orders WHERE order_id = ?", (req.order_id,)
        ).fetchone()

        if not order:
            raise HTTPException(status_code=404, detail="Order not found")

        if order["status"] == "VERIFIED":
            return {
                "verified": True,
                "status": "VERIFIED",
                "amount": order["amount"],
                "utr": order["utr"],
                "sender_name": order["sender_name"],
            }

    result = verifier.find_payment(
        expected_amount=order["amount"],
        note=order["note"],
        max_age_minutes=60
    )

    if result.get("verified"):
        with get_db() as conn:
            conn.execute(
                """UPDATE orders SET status='VERIFIED', utr=?, sender_name=?, verified_at=?
                   WHERE order_id=?""",
                (result.get("utr", ""), result.get("sender_name", ""), datetime.now().isoformat(), req.order_id)
            )
            conn.commit()

        return {
            "verified": True,
            "status": "VERIFIED",
            "amount": result.get("amount"),
            "utr": result.get("utr"),
            "sender_name": result.get("sender_name"),
        }
    else:
        return {
            "verified": False,
            "message": result.get("message", "Payment not found. Retry in 30-60 seconds."),
        }

@app.get("/order/{order_id}")
async def get_order(order_id: str):
    """Check order status without triggering verification."""
    with get_db() as conn:
        order = conn.execute(
            "SELECT * FROM orders WHERE order_id = ?", (order_id,)
        ).fetchone()
        if not order:
            raise HTTPException(status_code=404, detail="Order not found")
        return dict(order)
