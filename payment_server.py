import os
import json
import hmac
import time
import base64
import hashlib
import threading
import urllib.parse
from pathlib import Path
from datetime import datetime
from flask import Flask, request, jsonify
import requests

app = Flask(__name__)

# ==============================================================================
# 1. إعدادات فواتيرك والأمان (اضبطها من Environment Variables على Render/Koyeb)
# ==============================================================================
FAWATERK_API_KEY = os.environ.get("FAWATERK_API_KEY", "ضع_هنا_مفتاح_API_فواتيرك")
# للتجربة استخدم https://staging.fawaterk.com وللبيع الحقيقي غيّرها إلى https://app.fawaterk.com
FAWATERK_BASE_URL = os.environ.get("FAWATERK_BASE_URL", "https://staging.fawaterk.com")

# نفس المفتاح السري الموجود في bodydiskill.py
SIGNING_SECRET = os.environ.get("SIGNING_SECRET", "BODYDISKILL_PRO_SECRET_2026").encode("utf-8")

# كلمة سر لوحة تحكم المالك (لا تشاركها مع أحد)
ADMIN_KEY = os.environ.get("ADMIN_KEY", "BODY_ADMIN_998877")

# رابط السيرفر ده بعد رفعه
PUBLIC_SERVER_URL = os.environ.get("PUBLIC_SERVER_URL", "https://your-app.onrender.com").rstrip("/")

PRO_PRICE_EGP = int(os.environ.get("PRO_PRICE_EGP", "99"))

DB_FILE = Path("licenses_db.json")
DB_LOCK = threading.Lock()


# ==============================================================================
# 2. قاعدة البيانات المحلية + توليد التوكن الموقّع رقمياً
# ==============================================================================
def _load_db() -> dict:
    with DB_LOCK:
        if DB_FILE.exists():
            try:
                return json.loads(DB_FILE.read_text(encoding="utf-8"))
            except Exception:
                pass
        return {"users": {}}


def _save_db(db: dict):
    with DB_LOCK:
        DB_FILE.write_text(json.dumps(db, ensure_ascii=False, indent=2), encoding="utf-8")


def sign_license_token(hwid: str, buyer: str = "Pro User", license_type: str = "PAID") -> str:
    payload = {
        "hwid": hwid,
        "plan": "PRO_LIFETIME",
        "type": license_type,  # PAID أو VIP_GIFT
        "buyer": buyer,
        "last_verified": int(time.time()),
        "issued_at": datetime.now().strftime("%Y-%m-%d %H:%M")
    }
    payload_b64 = base64.urlsafe_b64encode(json.dumps(payload).encode("utf-8")).decode("ascii")
    sig = hmac.new(SIGNING_SECRET, payload_b64.encode("ascii"), hashlib.sha256).hexdigest()
    return f"{payload_b64}.{sig}"


def _check_admin(req) -> bool:
    key = req.args.get("key") or req.headers.get("X-Admin-Key") or ""
    return hmac.compare_digest(str(key), str(ADMIN_KEY))


# ==============================================================================
# 3. مسارات الدفع الحقيقي عبر Fawaterak (فوري + فيزا/ماستركارد + محافظ)
# ==============================================================================
@app.route("/api/create-payment", methods=["POST"])
def create_payment():
    data = request.get_json(silent=True) or {}
    hwid = (data.get("hwid") or "").strip().upper()
    customer_name = (data.get("name") or "BodyDisKill User").strip()
    customer_email = (data.get("email") or "user@bodydiskill.app").strip()
    customer_phone = (data.get("phone") or "01000000000").strip()

    if not hwid:
        return jsonify({"ok": False, "message": "كود الجهاز HWID مطلوب"}), 400

    payload = {
        "cartTotal": str(PRO_PRICE_EGP),
        "currency": "EGP",
        "customer": {
            "first_name": customer_name,
            "last_name": "Pro",
            "email": customer_email,
            "phone": customer_phone,
            "address": "Egypt"
        },
        "redirectionUrls": {
            "successUrl": f"{PUBLIC_SERVER_URL}/payment-success?hwid={urllib.parse.quote(hwid)}",
            "failUrl": f"{PUBLIC_SERVER_URL}/payment-failed",
            "pendingUrl": f"{PUBLIC_SERVER_URL}/payment-pending?hwid={urllib.parse.quote(hwid)}"
        },
        "cartItems": [
            {
                "name": f"BodyDisKill Pro Lifetime ({hwid})",
                "price": str(PRO_PRICE_EGP),
                "quantity": "1"
            }
        ],
        "payLoad": {"hwid": hwid, "buyer": customer_name}
    }

    try:
        resp = requests.post(
            f"{FAWATERK_BASE_URL}/api/v2/createInvoiceLink",
            headers={
                "Authorization": f"Bearer {FAWATERK_API_KEY}",
                "Content-Type": "application/json"
            },
            json=payload,
            timeout=15
        )
        res = resp.json()
        if res.get("status") == "success":
            return jsonify({
                "ok": True,
                "checkout_url": res["data"]["url"],
                "invoice_id": res["data"].get("invoiceId")
            })
        return jsonify({"ok": False, "message": res.get("message", "فشل إنشاء فاتورة فواتيرك")}), 400
    except Exception as e:
        return jsonify({"ok": False, "message": f"خطأ في الاتصال بفواتيرك: {e}"}), 500


@app.route("/webhook/fawaterk", methods=["POST"])
def fawaterk_webhook():
    """يستقبل إشعار الدفع التلقائي من فواتيرك ويفعّل الجهاز فوراً"""
    data = request.get_json(silent=True) or {}
    if str(data.get("invoice_status", "")).lower() == "paid":
        custom = data.get("pay_load") or {}
        if isinstance(custom, str):
            try:
                custom = json.loads(custom)
            except Exception:
                custom = {}
        hwid = (custom.get("hwid") or "").strip().upper()
        buyer = custom.get("buyer") or "عميل مدفوع"
        if hwid:
            db = _load_db()
            db["users"][hwid] = {
                "hwid": hwid,
                "buyer": buyer,
                "type": "PAID",
                "revoked": False,
                "amount_egp": PRO_PRICE_EGP,
                "payment_method": data.get("payment_method", "Fawaterak"),
                "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M")
            }
            _save_db(db)
    return jsonify({"received": True}), 200


@app.route("/api/sync-status", methods=["GET"])
def sync_status():
    """
    يتصل به برنامج BodyDisKill في الخلفية:
    - لو أضفت صاحبك من لوحة التحكم -> يفعّل برنامجه أوتوماتيك بدون ما ينسخ كود!
    - لو قفلت الاشتراك على صاحبك -> يقفل برنامجه ويرجّعه FREE فوراً!
    """
    hwid = (request.args.get("hwid") or "").strip().upper()
    if not hwid:
        return jsonify({"ok": False, "status": "free"})

    db = _load_db()
    rec = db["users"].get(hwid)
    if not rec:
        return jsonify({"ok": True, "status": "free"})

    if rec.get("revoked"):
        return jsonify({"ok": True, "status": "revoked", "message": "تم إيقاف الاشتراك من قبل المالك"})

    # تجديد التوكن بـ last_verified جديد عشان يشتغل أوفلاين بسلاسة
    fresh_token = sign_license_token(hwid, rec.get("buyer", "Pro User"), rec.get("type", "PAID"))
    return jsonify({
        "ok": True,
        "status": "active",
        "plan_type": rec.get("type", "PAID"),
        "token": fresh_token
    })


@app.route("/api/check-payment", methods=["GET"])
def check_payment():
    hwid = (request.args.get("hwid") or "").strip().upper()
    db = _load_db()
    rec = db["users"].get(hwid)
    if rec and not rec.get("revoked"):
        token = sign_license_token(hwid, rec.get("buyer", "Pro User"), rec.get("type", "PAID"))
        return jsonify({"ok": True, "paid": True, "token": token})
    return jsonify({"ok": True, "paid": False})


@app.route("/payment-success")
def payment_success():
    hwid = (request.args.get("hwid") or "").strip().upper()
    db = _load_db()
    rec = db["users"].get(hwid)
    if not rec or rec.get("revoked"):
        return """
        <html lang="ar" dir="rtl"><body style="background:#060a13;color:#fff;font-family:Segoe UI;text-align:center;padding:50px">
          <h2>⏳ جاري تأكيد الدفع من فواتيرك...</h2>
          <p>سيتم تفعيل البرنامج تلقائياً خلال ثوانٍ.</p>
          <script>setTimeout(()=>location.reload(), 2500);</script>
        </body></html>
        """
    token = sign_license_token(hwid, rec.get("buyer", "Pro User"), rec.get("type", "PAID"))
    deep_link = f"bodydiskill://activate?token={urllib.parse.quote(token)}"
    return f"""
    <html lang="ar" dir="rtl"><body style="background:#060a13;color:#fff;font-family:Segoe UI;text-align:center;padding:50px">
      <h1 style="color:#10b981">✓ تم الدفع والتفعيل بنجاح!</h1>
      <p>اضغط الزر بالأسفل لفتح <b>BodyDisKill Pro</b> وتفعيل كل الميزات فوراً:</p>
      <a href="{deep_link}" style="display:inline-block;margin-top:20px;padding:15px 30px;background:#10b981;color:#04111c;font-weight:800;border-radius:12px;text-decoration:none;font-size:18px">🚀 Open BodyDisKill Pro</a>
      <p style="margin-top:24px;font-size:12px;color:#94a3b8;word-break:break-all">كود احتياطي: {token}</p>
      <script>setTimeout(()=>{{ window.location.href = "{deep_link}"; }}, 600);</script>
    </body></html>
    """


@app.route("/payment-pending")
def payment_pending():
    return """
    <html lang="ar" dir="rtl"><body style="background:#060a13;color:#fff;font-family:Segoe UI;text-align:center;padding:50px">
      <h1 style="color:#f59e0b">⏳ في انتظار سداد كود فوري</h1>
      <p>ادفع الكود في أي ماكينة فوري، وبعدها افتح برنامج <b>BodyDisKill</b> وهيتفعل أوتوماتيك أو اضغط «تحقق من دفع فوري».</p>
    </body></html>
    """


@app.route("/payment-failed")
def payment_failed():
    return """
    <html lang="ar" dir="rtl"><body style="background:#060a13;color:#fff;font-family:Segoe UI;text-align:center;padding:50px">
      <h1 style="color:#ef4444">✖️ لم تكتمل عملية الدفع</h1>
      <p>يمكنك المحاولة مرة أخرى عبر البطاقة البنكية أو فوري.</p>
    </body></html>
    """


# ==============================================================================
# 4. لوحة تحكم المالك (فتح Pro مجاناً للأصدقاء والقرايب + قفلها في أي وقت)
# ==============================================================================
@app.route("/admin")
def admin_panel():
    if not _check_admin(request):
        return "غير مصرح بالدخول — أضف ?key=ADMIN_KEY في الرابط", 403
    key = urllib.parse.quote(request.args.get("key", ""))
    return f"""<!DOCTYPE html>
<html lang="ar" dir="rtl">
<head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>لوحة تحكم المالك — BodyDisKill Pro</title>
<style>
  *{{box-sizing:border-box;margin:0;padding:0;font-family:'Segoe UI',Tahoma,sans-serif}}
  body{{background:#060a13;color:#f1f5f9;padding:22px;max-width:920px;margin:auto}}
  .card{{background:#0f172a;border:1px solid rgba(148,163,184,.18);border-radius:16px;padding:20px;margin-bottom:18px}}
  h1{{font-size:22px;margin-bottom:6px;color:#38bdf8}}
  .muted{{color:#94a3b8;font-size:13px;margin-bottom:14px}}
  .stats{{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:12px;margin-bottom:18px}}
  .stat{{background:#131d35;padding:15px;border-radius:12px;text-align:center}}
  .stat b{{font-size:24px;color:#10b981;display:block;margin-top:4px}}
  .row{{display:flex;gap:10px;flex-wrap:wrap}}
  input{{background:#070c18;border:1px solid #334155;color:#fff;padding:11px 14px;border-radius:10px;font-size:14px;flex:1;min-width:180px}}
  button{{padding:11px 18px;border:none;border-radius:10px;font-weight:700;cursor:pointer;font-size:13px}}
  .btn-ok{{background:linear-gradient(90deg,#059669,#10b981);color:#fff}}
  .btn-err{{background:rgba(239,68,68,.2);color:#fca5a5;border:1px solid #ef4444}}
  .btn-cyan{{background:#0284c7;color:#fff}}
  .item{{display:flex;justify-content:space-between;align-items:center;gap:10px;flex-wrap:wrap;padding:12px;border-bottom:1px solid rgba(255,255,255,.07)}}
  .badge{{padding:3px 9px;border-radius:99px;font-size:11px;font-weight:700}}
  .b-vip{{background:rgba(99,102,241,.25);color:#a5b4fc}}
  .b-paid{{background:rgba(16,185,129,.2);color:#6ee7b7}}
  .b-rev{{background:rgba(239,68,68,.2);color:#fca5a5}}
  .link-box{{margin-top:12px;background:#040812;padding:12px;border-radius:10px;font-size:12px;word-break:break-all;border:1px dashed #38bdf8;display:none}}
</style>
</head>
<body>
  <div class="card">
    <h1>👑 لوحة تحكم المالك — BodyDisKill Pro</h1>
    <p class="muted">افتح خطة Pro مدى الحياة مجاناً لأصحابك وقرايبك عبر كود الجهاز (HWID) واقفلها عليهم في أي وقت بضغطة زر.</p>
    <div class="stats">
      <div class="stat">إجمالي المشتركين<b id="stTotal">0</b></div>
      <div class="stat">أصدقاء VIP مجاني<b id="stVip" style="color:#818cf8">0</b></div>
      <div class="stat">عملاء دفعوا (Fawaterak)<b id="stPaid">0</b></div>
      <div class="stat">أرباحك (ج.م)<b id="stMoney" style="color:#fbbf24">0</b></div>
    </div>
    <div class="row">
      <input id="fHwid" placeholder="كود جهاز صاحبك (مثال: OMN-A1B2-C3D4-E5F6)">
      <input id="fName" placeholder="الاسم (مثال: ياسين أخويا / أحمد صاحبي)">
      <button class="btn-ok" onclick="grantVip()">🎁 فتح Pro مجاناً مدى الحياة</button>
    </div>
    <div id="vipLinkBox" class="link-box"></div>
  </div>

  <div class="card">
    <h3 style="margin-bottom:10px">📋 قائمة الأجهزة والمشتركين (تحكم كامل بالفتح والقفل)</h3>
    <div id="usersList">جاري التحميل...</div>
  </div>

<script>
const KEY = "{key}";
async function loadUsers() {{
  const res = await fetch('/admin/api/list?key=' + KEY);
  const data = await res.json();
  document.getElementById('stTotal').textContent = data.stats.total;
  document.getElementById('stVip').textContent = data.stats.vip;
  document.getElementById('stPaid').textContent = data.stats.paid;
  document.getElementById('stMoney').textContent = data.stats.earnings + ' ج.م';

  const box = document.getElementById('usersList');
  if (!data.users.length) {{ box.innerHTML = '<p class="muted">لا يوجد مستخدمون مسجلون بعد.</p>'; return; }}
  box.innerHTML = data.users.map(u => {{
    const isRev = u.revoked;
    const badge = isRev ? '<span class="badge b-rev">مقفول (Revoked)</span>'
      : (u.type === 'VIP_GIFT' ? '<span class="badge b-vip">صديق VIP مجاني</span>' : '<span class="badge b-paid">مدفوع Fawaterak</span>');
    return `<div class="item">
      <div>
        <b>${{u.buyer}}</b> ${{badge}}
        <div style="font-family:monospace;font-size:12px;color:#94a3b8;margin-top:3px">${{u.hwid}} • ${{u.updated_at || ''}}</div>
      </div>
      <div class="row">
        ${{isRev
          ? `<button class="btn-ok" onclick="setRevoke('${{u.hwid}}', false)">🔓 إعادة فتح Pro</button>`
          : `<button class="btn-err" onclick="setRevoke('${{u.hwid}}', true)">🔒 قفل الاشتراك فوراً</button>`
        }}
        <button class="btn-cyan" onclick="copyToken('${{u.hwid}}')">📋 نسخ كود التفعيل</button>
      </div>
    </div>`;
  }}).join('');
}}

async function grantVip() {{
  const hwid = document.getElementById('fHwid').value.trim().toUpperCase();
  const buyer = document.getElementById('fName').value.trim() || 'صديق VIP';
  if (!hwid) return alert('اكتب كود جهاز صاحبك (HWID) أولاً');
  const res = await fetch('/admin/api/grant?key=' + KEY, {{
    method: 'POST', headers: {{'Content-Type': 'application/json'}},
    body: JSON.stringify({{hwid, buyer}})
  }});
  const data = await res.json();
  if (!data.ok) return alert(data.message);
  const lb = document.getElementById('vipLinkBox');
  lb.style.display = 'block';
  lb.innerHTML = `✅ <b>تم تفعيل الجهاز تلقائياً!</b> أول ما صاحبك يفتح البرنامج وهو متصل بالنت هيتحول Pro لوحده، أو ابعتله الكود ده:<br><br><code>${{data.token}}</code>`;
  document.getElementById('fHwid').value = '';
  document.getElementById('fName').value = '';
  loadUsers();
}}

async function setRevoke(hwid, revoke) {{
  await fetch('/admin/api/revoke?key=' + KEY, {{
    method: 'POST', headers: {{'Content-Type': 'application/json'}},
    body: JSON.stringify({{hwid, revoke}})
  }});
  loadUsers();
}}

async function copyToken(hwid) {{
  const res = await fetch('/admin/api/token?key=' + KEY + '&hwid=' + encodeURIComponent(hwid));
  const data = await res.json();
  if (data.token) {{
    await navigator.clipboard.writeText(data.token);
    alert('تم نسخ كود التفعيل! ابعته لصاحبك على واتساب.');
  }}
}}

loadUsers();
</script>
</body></html>"""


@app.route("/admin/api/list")
def admin_list():
    if not _check_admin(request):
        return jsonify({"ok": False}), 403
    db = _load_db()
    users = list(db["users"].values())
    vip_cnt = sum(1 for u in users if u.get("type") == "VIP_GIFT" and not u.get("revoked"))
    paid_cnt = sum(1 for u in users if u.get("type") == "PAID")
    earnings = sum(int(u.get("amount_egp", 0)) for u in users if u.get("type") == "PAID")
    return jsonify({
        "ok": True,
        "users": users,
        "stats": {"total": len(users), "vip": vip_cnt, "paid": paid_cnt, "earnings": earnings}
    })


@app.route("/admin/api/grant", methods=["POST"])
def admin_grant():
    if not _check_admin(request):
        return jsonify({"ok": False}), 403
    data = request.get_json(silent=True) or {}
    hwid = (data.get("hwid") or "").strip().upper()
    buyer = (data.get("buyer") or "صديق VIP").strip()
    if not hwid:
        return jsonify({"ok": False, "message": "كود الجهاز مطلوب"}), 400
    db = _load_db()
    db["users"][hwid] = {
        "hwid": hwid,
        "buyer": buyer,
        "type": "VIP_GIFT",
        "revoked": False,
        "amount_egp": 0,
        "payment_method": "هدية من المالك",
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M")
    }
    _save_db(db)
    token = sign_license_token(hwid, buyer, "VIP_GIFT")
    return jsonify({"ok": True, "token": token})


@app.route("/admin/api/revoke", methods=["POST"])
def admin_revoke():
    if not _check_admin(request):
        return jsonify({"ok": False}), 403
    data = request.get_json(silent=True) or {}
    hwid = (data.get("hwid") or "").strip().upper()
    revoke = bool(data.get("revoke", True))
    db = _load_db()
    if hwid in db["users"]:
        db["users"][hwid]["revoked"] = revoke
        db["users"][hwid]["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M")
        _save_db(db)
    return jsonify({"ok": True})


@app.route("/admin/api/token")
def admin_get_token():
    if not _check_admin(request):
        return jsonify({"ok": False}), 403
    hwid = (request.args.get("hwid") or "").strip().upper()
    db = _load_db()
    rec = db["users"].get(hwid)
    if not rec:
        return jsonify({"ok": False}), 404
    return jsonify({"ok": True, "token": sign_license_token(hwid, rec.get("buyer", "VIP"), rec.get("type", "VIP_GIFT"))})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))