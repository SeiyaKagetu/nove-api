"""
NOVE OS v13.2 - バックエンドAPI
FastAPI + SQLite
機能: お問い合わせフォーム処理 / ライセンスキー発行・管理
"""

from fastapi import FastAPI, HTTPException, Depends, Header, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, EmailStr
from typing import Optional
import sqlite3
import uuid
import os
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime, timedelta
from dotenv import load_dotenv

load_dotenv()

# Resendはオプション（インストールされていれば使用）
try:
    import resend as _resend_module
    _RESEND_AVAILABLE = True
except ImportError:
    _RESEND_AVAILABLE = False

app = FastAPI(
    title="NOVE OS API",
    description="NOVE OS v13.2 バックエンドAPI",
    version="1.2.0"
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
    conn.execute("""
        CREATE TABLE IF NOT EXISTS activations (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            license_key  TEXT NOT NULL,
            machine_id   TEXT NOT NULL,
            activated_at TEXT DEFAULT (datetime('now', 'localtime')),
            last_seen    TEXT DEFAULT (datetime('now', 'localtime')),
            UNIQUE(license_key, machine_id)
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
# メール送信（Resend優先 → SMTP(Gmail)フォールバック）
# ─────────────────────────────
RESEND_API_KEY = os.getenv("RESEND_API_KEY", "")
MAIL_FROM      = os.getenv("MAIL_FROM", "NOVE OS <noreply@noveos.jp>")
NOTIFY_TO      = os.getenv("NOTIFY_TO", "myseiyakagetu@proton.me")

# SMTP設定（Gmail App Password等）
SMTP_HOST = os.getenv("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASS = os.getenv("SMTP_PASS", "")


def _send_via_resend(to: str, subject: str, body: str):
    """Resend APIでメール送信"""
    if not _RESEND_AVAILABLE:
        raise RuntimeError("resend package not installed")
    _resend_module.api_key = RESEND_API_KEY
    _resend_module.Emails.send({
        "from": MAIL_FROM,
        "to": [to],
        "subject": subject,
        "html": body,
    })


def _send_via_smtp(to: str, subject: str, body: str):
    """SMTP(Gmail等)でメール送信"""
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = SMTP_USER
    msg["To"]      = to
    msg.attach(MIMEText(body, "html", "utf-8"))
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=15) as s:
        s.ehlo()
        s.starttls()
        s.login(SMTP_USER, SMTP_PASS)
        s.sendmail(SMTP_USER, [to], msg.as_string())


def send_email(to: str, subject: str, body: str):
    """Resend優先 → SMTPフォールバックでメール送信"""
    # Resend優先
    if RESEND_API_KEY and _RESEND_AVAILABLE:
        try:
            _send_via_resend(to, subject, body)
            print(f"[MAIL OK/Resend] To:{to} Subject:{subject}")
            return
        except Exception as e:
            print(f"[MAIL WARN/Resend] {e} → SMTPへフォールバック")

    # SMTPフォールバック
    if SMTP_USER and SMTP_PASS:
        try:
            _send_via_smtp(to, subject, body)
            print(f"[MAIL OK/SMTP] To:{to} Subject:{subject}")
            return
        except Exception as e:
            print(f"[MAIL ERROR/SMTP] {e}")
    else:
        print(f"[MAIL SKIP] メール設定未完了 - To:{to} Subject:{subject}")

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
    "trial14":     ("14日間無料トライアル", 1, "無料"),
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

class LicenseActivate(BaseModel):
    license_key: str
    machine_id:  str

class TrialRequest(BaseModel):
    name:    str
    email:   EmailStr
    company: Optional[str] = None

# ─────────────────────────────
# トライアルAPI（公開）
# ─────────────────────────────
@app.post("/api/trial/request", summary="14日間無料トライアル申込（公開）")
async def request_trial(data: TrialRequest, background_tasks: BackgroundTasks, db: sqlite3.Connection = Depends(get_db)):
    # 同一メールでのトライアル重複チェック
    existing = db.execute(
        "SELECT id FROM licenses WHERE customer_email=? AND plan='trial14'",
        (data.email,)
    ).fetchone()
    if existing:
        raise HTTPException(status_code=409, detail="このメールアドレスはすでにトライアルを使用済みです")

    plan_name, server_limit, _ = PLAN_LABELS["trial14"]
    key = generate_key("trial14")
    valid_from  = datetime.now().strftime("%Y-%m-%d")
    valid_until = (datetime.now() + timedelta(days=14)).strftime("%Y-%m-%d")

    try:
        db.execute(
            """INSERT INTO licenses(license_key,plan,customer_name,customer_email,
               server_limit,valid_from,valid_until,note)
               VALUES(?,?,?,?,?,?,?,?)""",
            (key, "trial14", data.name, data.email,
             server_limit, valid_from, valid_until, f"会社: {data.company or '未記入'}")
        )
        db.commit()
    except sqlite3.IntegrityError:
        raise HTTPException(status_code=500, detail="キー生成に失敗しました。再試行してください。")

    # コンタクト保存
    db.execute(
        "INSERT INTO contacts(user_type,name,email,company,plan,message) VALUES(?,?,?,?,?,?)",
        ("トライアル", data.name, data.email, data.company, "trial14", "14日間無料トライアル申込")
    )
    db.commit()

    install_cmd = f"curl -fsSL https://noveos.jp/install.sh | sudo bash -s {key}"

    # ユーザーへメール
    user_body = f"""
<div style="font-family:sans-serif;max-width:600px;margin:0 auto;background:#0d1117;color:#f0f6fc;padding:24px;border-radius:12px;">
<h2 style="color:#30d158;">🎉 NOVE OS v13.2 14日間無料トライアル開始！</h2>
<p>{data.name} 様</p>
<p>14日間無料トライアルへのご参加ありがとうございます。<br>
Rocky Linux NOVE OS v13.2 チームです。</p>
<table border="1" cellpadding="10" style="border-collapse:collapse;min-width:400px;margin:16px 0;">
<tr style="background:#0071e3;color:#fff;"><th colspan="2" style="padding:12px;">トライアル情報</th></tr>
<tr style="background:#161b22;"><th style="color:#8b949e;text-align:left;padding:10px;">ライセンスキー</th>
    <td><strong style="font-size:17px;font-family:monospace;color:#ffd60a;">{key}</strong></td></tr>
<tr style="background:#0d1117;"><th style="color:#8b949e;text-align:left;padding:10px;">有効期間</th>
    <td style="color:#f0f6fc;">{valid_from} 〜 <strong>{valid_until}</strong>（14日間）</td></tr>
<tr style="background:#161b22;"><th style="color:#8b949e;text-align:left;padding:10px;">対応サーバー</th>
    <td style="color:#f0f6fc;">1台</td></tr>
</table>
<p style="margin-top:20px;"><strong>📦 インストール方法（Rocky Linux / RHEL系）:</strong></p>
<pre style="background:#1f2937;color:#30d158;padding:14px;border-radius:8px;overflow-x:auto;font-size:13px;">{install_cmd}</pre>
<hr style="border-color:#30303a;margin:24px 0;">
<p style="color:#8b949e;font-size:13px;">
ご不明な点はお気軽にお問い合わせください。<br>
トライアル終了後はそのままご契約いただけます。<br><br>
NOVE OS Systems | <a href="https://noveos.jp" style="color:#0071e3;">https://noveos.jp</a> | myseiyakagetu@proton.me
</p>
</div>
"""
    background_tasks.add_task(
        send_email, data.email,
        "【NOVE OS】14日間無料トライアル開始 - ライセンスキーのご案内",
        user_body
    )
    background_tasks.add_task(
        send_email, NOTIFY_TO,
        f"【トライアル申込】{data.name}様 / {data.email}",
        f"Key: {key}<br>Company: {data.company or '-'}<br>Valid: {valid_until}"
    )

    return {
        "status":      "ok",
        "license_key": key,
        "valid_until": valid_until,
        "install_cmd": install_cmd,
    }


# ─────────────────────────────
# お問い合わせAPI
# ─────────────────────────────
@app.post("/api/contact", summary="お問い合わせ送信")
async def submit_contact(form: ContactForm, background_tasks: BackgroundTasks, db: sqlite3.Connection = Depends(get_db)):
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
    background_tasks.add_task(send_email, NOTIFY_TO, f"【お問い合わせ】{form.user_type} / {form.name}様", admin_body)

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
    background_tasks.add_task(send_email, form.email, "【受付完了】お問い合わせありがとうございます - NOVE OS", reply_body)

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
async def create_license(data: LicenseCreate, background_tasks: BackgroundTasks, admin=Depends(verify_admin), db: sqlite3.Connection = Depends(get_db)):
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
    background_tasks.add_task(send_email, data.customer_email, f"【NOVE OS】ライセンスキーのご案内 - {plan_name}", mail_body)
    background_tasks.add_task(send_email, NOTIFY_TO, f"【発行完了】{data.customer_name}様 / {plan_name}", f"Key: {key}<br>Email: {data.customer_email}")

    return {
        "status": "ok",
        "license_key": key,
        "plan": plan_name,
        "customer_email": data.customer_email,
        "valid_from": valid_from,
        "valid_until": valid_until,
        "server_limit": server_limit
    }


@app.post("/api/license/activate", summary="ライセンス認証・マシン登録")
async def activate_license(data: LicenseActivate, db: sqlite3.Connection = Depends(get_db)):
    """
    インストーラーから呼ばれるエンドポイント。
    1. ライセンスキーの有効性を確認
    2. サーバー台数上限チェック（同一マシンは重複カウントしない）
    3. マシンIDを activations テーブルに記録
    4. プラン情報を返す
    """
    row = db.execute(
        "SELECT * FROM licenses WHERE license_key=?", (data.license_key,)
    ).fetchone()

    if not row:
        raise HTTPException(status_code=404, detail="ライセンスキーが見つかりません")

    lic = dict(row)
    today = datetime.now().strftime("%Y-%m-%d")

    if not lic["is_active"]:
        raise HTTPException(status_code=403, detail="このライセンスは無効化されています")

    if lic["valid_until"] < today:
        raise HTTPException(
            status_code=403,
            detail=f"ライセンスの有効期限が切れています（期限: {lic['valid_until']}）"
        )

    # 同一マシンはすでに登録済みか確認
    already = db.execute(
        "SELECT id FROM activations WHERE license_key=? AND machine_id=?",
        (data.license_key, data.machine_id)
    ).fetchone()

    if not already:
        # 現在のアクティベーション台数を確認
        count = db.execute(
            "SELECT COUNT(*) FROM activations WHERE license_key=?",
            (data.license_key,)
        ).fetchone()[0]

        server_limit = lic["server_limit"]
        # server_limit=0 は無制限（無料相談・trial等）
        if server_limit > 0 and count >= server_limit:
            raise HTTPException(
                status_code=403,
                detail=(
                    f"サーバー台数の上限（{server_limit}台）に達しています。"
                    "ライセンスのアップグレードはサポート(myseiyakagetu@proton.me)までご連絡ください。"
                )
            )

        # 新規マシンを登録
        db.execute(
            "INSERT INTO activations(license_key, machine_id) VALUES(?,?)",
            (data.license_key, data.machine_id)
        )
        db.commit()
        status = "activated"
    else:
        # 既存マシン: last_seen を更新
        db.execute(
            "UPDATE activations SET last_seen=datetime('now','localtime') "
            "WHERE license_key=? AND machine_id=?",
            (data.license_key, data.machine_id)
        )
        db.commit()
        status = "valid"

    # アクティベーション済み台数
    activated_count = db.execute(
        "SELECT COUNT(*) FROM activations WHERE license_key=?",
        (data.license_key,)
    ).fetchone()[0]

    return {
        "is_valid":        True,
        "status":          status,
        "plan":            lic["plan"],
        "customer_name":   lic["customer_name"],
        "valid_until":     lic["valid_until"],
        "server_limit":    lic["server_limit"],
        "activated_count": activated_count,
    }


@app.get("/api/license/{key}/activations", summary="マシン一覧（管理者）")
async def list_activations(key: str, admin=Depends(verify_admin), db: sqlite3.Connection = Depends(get_db)):
    rows = db.execute(
        "SELECT * FROM activations WHERE license_key=? ORDER BY activated_at DESC", (key,)
    ).fetchall()
    return [dict(r) for r in rows]


@app.delete("/api/license/{key}/activations/{machine_id}", summary="マシン登録解除（管理者）")
async def remove_activation(key: str, machine_id: str, admin=Depends(verify_admin), db: sqlite3.Connection = Depends(get_db)):
    db.execute(
        "DELETE FROM activations WHERE license_key=? AND machine_id=?", (key, machine_id)
    )
    db.commit()
    return {"status": "ok", "message": f"マシン {machine_id} の登録を解除しました"}


@app.get("/api/license/validate/{key}", summary="ライセンス有効性確認")
async def validate_license(key: str, db: sqlite3.Connection = Depends(get_db)):
    row = db.execute("SELECT * FROM licenses WHERE license_key=?", (key,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="ライセンスキーが見つかりません")
    r = dict(row)
    today = datetime.now().strftime("%Y-%m-%d")
    r["is_expired"] = (r["valid_until"] < today)
    r["is_valid"]   = bool(r["is_active"]) and not r["is_expired"]
    # アクティベーション台数を追加
    count = db.execute(
        "SELECT COUNT(*) FROM activations WHERE license_key=?", (key,)
    ).fetchone()[0]
    r["activated_count"] = count
    return r


@app.get("/api/licenses", summary="ライセンス一覧（管理者）")
async def list_licenses(admin=Depends(verify_admin), db: sqlite3.Connection = Depends(get_db)):
    rows = db.execute("SELECT * FROM licenses ORDER BY created_at DESC").fetchall()
    result = []
    for row in rows:
        r = dict(row)
        count = db.execute(
            "SELECT COUNT(*) FROM activations WHERE license_key=?", (r["license_key"],)
        ).fetchone()[0]
        r["activated_count"] = count
        result.append(r)
    return result


@app.delete("/api/license/{key}", summary="ライセンス無効化（管理者）")
async def revoke_license(key: str, admin=Depends(verify_admin), db: sqlite3.Connection = Depends(get_db)):
    db.execute("UPDATE licenses SET is_active=0 WHERE license_key=?", (key,))
    db.commit()
    return {"status": "ok", "message": f"{key} を無効化しました"}


@app.get("/", summary="ヘルスチェック")
async def root():
    return {"status": "ok", "service": "NOVE OS API v1.2", "docs": "/docs"}
