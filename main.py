"""
NOVE OS v13.2 - バックエンドAPI
FastAPI + SQLite
機能: お問い合わせフォーム処理 / ライセンスキー発行・管理
"""

from fastapi import FastAPI, HTTPException, Depends, Header
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, EmailStr
from typing import Optional
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import sqlite3
import uuid
import hashlib
import os
from datetime import datetime, timedelta
from dotenv import load_dotenv

load_dotenv()

app = FastAPI(
    title="NOVE OS API",
    description="NOVE OS v13.2 バックエンドAPI",
    version="1.0.0"
)

# CORS設定（noveos.jpからのリクエストを許可）
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://noveos.jp",
        "https://*.netlify.app",
        "http://localhost:8080",
        "http://localhost:3000",
    ],
    allow_methods=["GET", "POST", "DELETE"],
    allow_headers=["*"],
)

# ─────────────────────────────
# データベース初期化
# ─────────────────────────────
DB_PATH = os.getenv("DB_PATH", "nove_os.db")

def get_db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()

def init_db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS contacts (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            user_type TEXT NOT NULL,
            name      TEXT NOT NULL,
            email     TEXT NOT NULL,
            company   TEXT,
            plan      TEXT,
            message   TEXT,
            created_at TEXT DEFAULT (datetime('now', 'localtime'))
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS licenses (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            license_key  TEXT UNIQUE NOT NULL,
            plan         TEXT NOT NULL,
            customer_name TEXT NOT NULL,
            customer_email TEXT NOT NULL,
            server_limit INTEGER NOT NULL,
            valid_from   TEXT NOT NULL,
            valid_until  TEXT NOT NULL,
            is_active    INTEGER DEFAULT 1,
            note         TEXT,
            created_at   TEXT DEFAULT (datetime('now', 'localtime'))
        )
    """)
    conn.commit()
    conn.close()

init_db()

# ─────────────────────────────
# 管理者認証
# ─────────────────────────────
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "change-this-secret-token")

def verify_admin(x_admin_token: str = Header(...)):
    if x_admin_token != ADMIN_TOKEN:
        raise HTTPException(status_code=401, detail="認証エラー")
    return True

# ─────────────────────────────
# メール送信
# ─────────────────────────────
SMTP_HOST   = os.getenv("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT   = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER   = os.getenv("SMTP_USER", "")
SMTP_PASS   = os.getenv("SMTP_PASS", "")
NOTIFY_TO   = os.getenv("NOTIFY_TO", "myseiyakagetu@proton.me")

def send_email(to: str, subject: str, body: str):
    if not SMTP_USER or not SMTP_PASS:
        print(f"[MAIL SKIP] To:{to} Subject:{subject}")
        return
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = SMTP_USER
    msg["To"] = to
    msg.attach(MIMEText(body, "html", "utf-8"))
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
        server.starttls()
        server.login(SMTP_USER, SMTP_PASS)
        server.sendmail(SMTP_USER, to, msg.as_string())

# ─────────────────────────────
# モデル定義
# ─────────────────────────────
class ContactForm(BaseModel):
    user_type:    str               # 法人・個人事業主・個人
    name:         str
    email:        EmailStr
    company:      Optional[str] = None
    position:     Optional[str] = None
    business_name: Optional[str] = None
    industry:     Optional[str] = None
    plan:         Optional[str] = None
    servers:      Optional[int] = None
    timeline:     Optional[str] = None
    purpose:      Optional[str] = None
    message:      str

PLAN_LABELS = {
    "personal":    ("パーソナル",     3,     "¥5,000/月"),
    "academic":    ("アカデミック",   10,    "¥50,000/月"),
    "startup":     ("スタートアップ", 50,    "¥200,000/月"),
    "standard":    ("スタンダード",   500,   "¥1,000,000/月"),
    "enterprise":  ("エンタープライズ", 99999, "¥1,500,000~/月"),
    "beta":        ("ベータテスト",   50,    "50%割引"),
    "trial":       ("お試し相談",     0,     "無料"),
    "consultation":("無料相談",       0,     "無料"),
    "other":       ("その他",         0,     "-"),
}

class LicenseCreate(BaseModel):
    plan:           str
    customer_name:  str
    customer_email: EmailStr
    months:         int = 12
    note:           Optional[str] = None

# ─────────────────────────────
# お問い合わせAPI
# ─────────────────────────────
@app.post("/api/contact", summary="お問い合わせ送信")
async def submit_contact(form: ContactForm, db: sqlite3.Connection = Depends(get_db)):
    # DB保存
    db.execute(
        "INSERT INTO contacts(user_type,name,email,company,plan,message) VALUES(?,?,?,?,?,?)",
        (form.user_type, form.name, form.email, form.company or form.business_name, form.plan, form.message)
    )
    db.commit()

    # 管理者宛メール
    admin_body = f"""
<h2>📬 新しいお問い合わせ</h2>
<table border="1" cellpadding="8" style="border-collapse:collapse;">
<tr><th>種別</th><td>{form.user_type}</td></tr>
<tr><th>お名前</th><td>{form.name}</td></tr>
<tr><th>メール</th><td>{form.email}</td></tr>
<tr><th>会社/屋号</th><td>{form.company or form.business_name or '-'}</td></tr>
<tr><th>プラン</th><td>{form.plan or '-'}</td></tr>
<tr><th>台数</th><td>{form.servers or '-'}</td></tr>
<tr><th>時期</th><td>{form.timeline or '-'}</td></tr>
<tr><th>内容</th><td>{form.message}</td></tr>
</table>
<p style="color:#666;font-size:12px;">NOVE OS API - {datetime.now().strftime('%Y-%m-%d %H:%M')}</p>
"""
    send_email(NOTIFY_TO, f"【お問い合わせ】{form.user_type} / {form.name}様", admin_body)

    # 自動返信メール
    reply_body = f"""
<p>{form.name} 様</p>
<p>お問い合わせありがとうございます。<br>
Rocky Linux NOVE OS v13.2 チームです。</p>
<p>以下の内容でお問い合わせを受け付けました。<br>
<strong>1営業日以内</strong>にご返信いたします。</p>
<hr>
<p><strong>ご送信内容：</strong><br>{form.message}</p>
<hr>
<p style="color:#666;font-size:12px;">
NOVE OS Systems | myseiyakagetu@proton.me<br>
<a href="https://noveos.jp">https://noveos.jp</a>
</p>
"""
    send_email(form.email, "【受付完了】お問い合わせありがとうございます - NOVE OS", reply_body)

    return {"status": "ok", "message": "送信完了しました"}


@app.get("/api/contacts", summary="お問い合わせ一覧（管理者）")
async def list_contacts(admin=Depends(verify_admin), db: sqlite3.Connection = Depends(get_db)):
    rows = db.execute("SELECT * FROM contacts ORDER BY created_at DESC").fetchall()
    return [dict(r) for r in rows]


# ─────────────────────────────
# ライセンスキーAPI
# ─────────────────────────────
def generate_key(plan: str) -> str:
    raw = uuid.uuid4().hex.upper()
    return f"NOVE-{plan[:3].upper()}-{raw[:4]}-{raw[4:8]}-{raw[8:12]}"


@app.post("/api/license/generate", summary="ライセンスキー発行（管理者）")
async def create_license(data: LicenseCreate, admin=Depends(verify_admin), db: sqlite3.Connection = Depends(get_db)):
    plan_info = PLAN_LABELS.get(data.plan)
    if not plan_info:
        raise HTTPException(status_code=400, detail="不明なプランです")

    plan_name, server_limit, price = plan_info
    key = generate_key(data.plan)
    valid_from  = datetime.now().strftime("%Y-%m-%d")
    valid_until = (datetime.now() + timedelta(days=30 * data.months)).strftime("%Y-%m-%d")

    try:
        db.execute(
            """INSERT INTO licenses(license_key,plan,customer_name,customer_email,
               server_limit,valid_from,valid_until,note)
               VALUES(?,?,?,?,?,?,?,?)""",
            (key, data.plan, data.customer_name, data.customer_email,
             server_limit, valid_from, valid_until, data.note)
        )
        db.commit()
    except sqlite3.IntegrityError:
        raise HTTPException(status_code=500, detail="キー生成に失敗しました。再試行してください。")

    # ライセンスメール送信
    mail_body = f"""
<h2>🎉 NOVE OS v13.2 ライセンスキーのご案内</h2>
<p>{data.customer_name} 様</p>
<p>この度はNOVE OS v13.2をご購入いただきありがとうございます。</p>
<table border="1" cellpadding="10" style="border-collapse:collapse; min-width:400px;">
<tr style="background:#0071e3;color:#fff;"><th colspan="2">ライセンス情報</th></tr>
<tr><th>ライセンスキー</th><td><strong style="font-size:18px;font-family:monospace;">{key}</strong></td></tr>
<tr><th>プラン</th><td>{plan_name}（{price}）</td></tr>
<tr><th>サーバー上限</th><td>{server_limit}台</td></tr>
<tr><th>有効期間</th><td>{valid_from} 〜 {valid_until}</td></tr>
</table>
<br>
<p>ライセンスキーは大切に保管してください。<br>
ご不明な点はお気軽にお問い合わせください。</p>
<p style="color:#666;font-size:12px;">
NOVE OS Systems | <a href="https://noveos.jp">https://noveos.jp</a>
</p>
"""
    send_email(data.customer_email, f"【NOVE OS】ライセンスキーのご案内 - {plan_name}", mail_body)
    send_email(NOTIFY_TO, f"【発行完了】{data.customer_name}様 / {plan_name}", f"Key: {key}<br>Email: {data.customer_email}")

    return {
        "status": "ok",
        "license_key": key,
        "plan": plan_name,
        "customer_email": data.customer_email,
        "valid_from": valid_from,
        "valid_until": valid_until,
        "server_limit": server_limit
    }


@app.get("/api/license/validate/{key}", summary="ライセンス有効性確認")
async def validate_license(key: str, db: sqlite3.Connection = Depends(get_db)):
    row = db.execute("SELECT * FROM licenses WHERE license_key=?", (key,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="ライセンスキーが見つかりません")
    r = dict(row)
    today = datetime.now().strftime("%Y-%m-%d")
    r["is_expired"] = (r["valid_until"] < today)
    r["is_valid"]   = bool(r["is_active"]) and not r["is_expired"]
    return r


@app.get("/api/licenses", summary="ライセンス一覧（管理者）")
async def list_licenses(admin=Depends(verify_admin), db: sqlite3.Connection = Depends(get_db)):
    rows = db.execute("SELECT * FROM licenses ORDER BY created_at DESC").fetchall()
    return [dict(r) for r in rows]


@app.delete("/api/license/{key}", summary="ライセンス無効化（管理者）")
async def revoke_license(key: str, admin=Depends(verify_admin), db: sqlite3.Connection = Depends(get_db)):
    db.execute("UPDATE licenses SET is_active=0 WHERE license_key=?", (key,))
    db.commit()
    return {"status": "ok", "message": f"{key} を無効化しました"}


@app.get("/", summary="ヘルスチェック")
async def root():
    return {"status": "ok", "service": "NOVE OS API v1.0", "docs": "/docs"}
