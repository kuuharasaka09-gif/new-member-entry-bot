import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from html import escape
from pathlib import Path

import asyncpg
from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware
from telegram import ChatJoinRequest, ReplyKeyboardMarkup, Update
from telegram.ext import (
    Application,
    ChatJoinRequestHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

BOT_TOKEN = os.environ["BOT_TOKEN"]
DATABASE_URL = os.environ["DATABASE_URL"]
GROUP_ID = int(os.environ["GROUP_ID"])
ADMIN_ID = int(os.getenv("ADMIN_ID", "8245808922"))
ADMIN_PASSWORD = os.environ["ADMIN_PASSWORD"]
SECRET_KEY = os.environ["SECRET_KEY"]
MEMBERSHIP_FEE = int(os.getenv("MEMBERSHIP_FEE", "3000"))
QR_EXPIRES_AT = os.getenv("QR_EXPIRES_AT", "2026-08-05 07:09 JST")

BASE_DIR = Path(__file__).resolve().parent
QR_PATH = BASE_DIR / "assets" / "paypay_qr.jpg"

MENU = ReplyKeyboardMarkup(
    [
        ["💳 PayPayで支払う"],
        ["✅ 入金完了"],
        ["🚪 グループへ入室する"],
        ["🔄 承認状況を確認"],
    ],
    resize_keyboard=True,
)

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

pool: asyncpg.Pool | None = None
telegram_app: Application | None = None


def yen(value: int) -> str:
    return f"{value:,}円"


async def init_db(db: asyncpg.Pool) -> None:
    async with db.acquire() as conn:
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS members (
                telegram_id BIGINT PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                is_approved BOOLEAN NOT NULL DEFAULT FALSE,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                approved_at TIMESTAMPTZ
            );

            CREATE TABLE IF NOT EXISTS payment_requests (
                id BIGSERIAL PRIMARY KEY,
                telegram_id BIGINT NOT NULL,
                amount INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                processed_at TIMESTAMPTZ,
                reject_reason TEXT
            );

            CREATE TABLE IF NOT EXISTS entry_links (
                id BIGSERIAL PRIMARY KEY,
                telegram_id BIGINT NOT NULL,
                invite_link TEXT NOT NULL UNIQUE,
                expires_at TIMESTAMPTZ NOT NULL,
                used BOOLEAN NOT NULL DEFAULT FALSE,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );

            CREATE INDEX IF NOT EXISTS idx_payment_requests_status
            ON payment_requests(status);
            """
        )


async def ensure_member(update: Update) -> asyncpg.Record:
    assert pool is not None
    user = update.effective_user
    assert user is not None

    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM members WHERE telegram_id=$1",
            user.id,
        )
        if row:
            await conn.execute(
                """
                UPDATE members
                SET username=$2, first_name=$3
                WHERE telegram_id=$1
                """,
                user.id,
                user.username,
                user.first_name,
            )
            return await conn.fetchrow(
                "SELECT * FROM members WHERE telegram_id=$1",
                user.id,
            )

        return await conn.fetchrow(
            """
            INSERT INTO members(telegram_id, username, first_name)
            VALUES($1,$2,$3)
            RETURNING *
            """,
            user.id,
            user.username,
            user.first_name,
        )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    member = await ensure_member(update)
    status = "✅ 承認済み" if member["is_approved"] else "⏳ 入金確認待ち"

    await update.effective_message.reply_text(
        "新規会員入室Botへようこそ。\n\n"
        f"入会費：{yen(MEMBERSHIP_FEE)}\n"
        f"現在の状態：{status}\n\n"
        "1. PayPayで支払う\n"
        "2. 入金完了を押す\n"
        "3. 管理者の承認後、入室リンクを発行",
        reply_markup=MENU,
    )


async def show_qr(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await ensure_member(update)

    with QR_PATH.open("rb") as image:
        await update.effective_message.reply_photo(
            photo=image,
            caption=(
                f"💳 入会費：{yen(MEMBERSHIP_FEE)}\n\n"
                "このQRコードをPayPayで読み取り、3,000円を送金してください。\n"
                "送金後に「✅ 入金完了」を押してください。\n\n"
                f"QR有効期限：{QR_EXPIRES_AT}"
            ),
            reply_markup=MENU,
        )


async def payment_done(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    assert pool is not None
    member = await ensure_member(update)

    if member["is_approved"]:
        await update.effective_message.reply_text(
            "すでに承認済みです。",
            reply_markup=MENU,
        )
        return

    async with pool.acquire() as conn:
        pending = await conn.fetchval(
            """
            SELECT EXISTS(
                SELECT 1 FROM payment_requests
                WHERE telegram_id=$1 AND status='pending'
            )
            """,
            update.effective_user.id,
        )
        if pending:
            await update.effective_message.reply_text(
                "すでに入金確認待ちです。管理者の確認をお待ちください。",
                reply_markup=MENU,
            )
            return

        request_id = await conn.fetchval(
            """
            INSERT INTO payment_requests(telegram_id, amount)
            VALUES($1,$2)
            RETURNING id
            """,
            update.effective_user.id,
            MEMBERSHIP_FEE,
        )

    username = (
        f"@{update.effective_user.username}"
        if update.effective_user.username
        else "なし"
    )

    await update.effective_message.reply_text(
        "✅ 入金完了メッセージを送信しました。\n\n"
        f"申請番号：{request_id}\n"
        "管理者がPayPay入金履歴を確認します。",
        reply_markup=MENU,
    )

    try:
        await context.bot.send_message(
            ADMIN_ID,
            "🟡 新しい入金確認申請\n\n"
            f"申請番号：{request_id}\n"
            f"会員名：{update.effective_user.first_name}\n"
            f"ユーザー名：{username}\n"
            f"Telegram ID：{update.effective_user.id}\n"
            f"金額：{yen(MEMBERSHIP_FEE)}\n\n"
            "Web管理画面から承認してください。",
        )
    except Exception:
        logger.exception("管理者通知に失敗しました")


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    member = await ensure_member(update)
    await update.effective_message.reply_text(
        "✅ 入金確認済みです。入室リンクを発行できます。"
        if member["is_approved"]
        else "⏳ 現在は入金確認待ちです。",
        reply_markup=MENU,
    )


async def create_entry_link(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    assert pool is not None
    member = await ensure_member(update)
    user_id = update.effective_user.id

    if not member["is_approved"]:
        await update.effective_message.reply_text(
            "まだ入金確認が完了していません。",
            reply_markup=MENU,
        )
        return

    expires_at = datetime.now(timezone.utc) + timedelta(minutes=10)

    try:
        invite = await context.bot.create_chat_invite_link(
            chat_id=GROUP_ID,
            name=f"member-{user_id}",
            expire_date=expires_at,
            creates_join_request=True,
        )
    except Exception:
        logger.exception("入室リンク作成失敗")
        await update.effective_message.reply_text(
            "入室リンクを発行できませんでした。\n"
            "管理者へお問い合わせください。",
            reply_markup=MENU,
        )
        return

    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO entry_links(telegram_id, invite_link, expires_at)
            VALUES($1,$2,$3)
            """,
            user_id,
            invite.invite_link,
            expires_at,
        )

    await update.effective_message.reply_text(
        "🎫 本人専用の入室リンクです。\n\n"
        f"{invite.invite_link}\n\n"
        "有効期限：10分\n"
        "このリンクを他人に渡しても、その人は承認されません。",
        reply_markup=MENU,
    )


async def join_request(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    assert pool is not None
    request: ChatJoinRequest | None = update.chat_join_request
    if request is None:
        return

    invite_link = request.invite_link.invite_link if request.invite_link else None

    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT id
            FROM entry_links
            WHERE telegram_id=$1
              AND invite_link=$2
              AND expires_at > NOW()
              AND used=FALSE
            ORDER BY id DESC
            LIMIT 1
            """,
            request.from_user.id,
            invite_link,
        )

        if row:
            await conn.execute(
                "UPDATE entry_links SET used=TRUE WHERE id=$1",
                row["id"],
            )

    if row:
        await request.approve()
        await context.bot.send_message(
            request.from_user.id,
            "✅ 本人確認が完了しました。グループへの参加を承認しました。",
            reply_markup=MENU,
        )
    else:
        await request.decline()
        try:
            await context.bot.send_message(
                request.from_user.id,
                "❌ 本人確認ができなかったため、参加申請を拒否しました。",
                reply_markup=MENU,
            )
        except Exception:
            logger.exception("拒否通知に失敗しました")


async def text_router(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = update.effective_message.text or ""

    if text == "💳 PayPayで支払う":
        await show_qr(update, context)
    elif text == "✅ 入金完了":
        await payment_done(update, context)
    elif text == "🚪 グループへ入室する":
        await create_entry_link(update, context)
    elif text == "🔄 承認状況を確認":
        await status(update, context)
    else:
        await update.effective_message.reply_text(
            "下のメニューから選んでください。",
            reply_markup=MENU,
        )


async def approve_request(request_id: int) -> tuple[bool, str]:
    assert pool is not None and telegram_app is not None

    async with pool.acquire() as conn:
        async with conn.transaction():
            payment = await conn.fetchrow(
                """
                SELECT *
                FROM payment_requests
                WHERE id=$1
                FOR UPDATE
                """,
                request_id,
            )

            if not payment:
                return False, "申請が見つかりません。"
            if payment["status"] != "pending":
                return False, "すでに処理済みです。"

            await conn.execute(
                """
                UPDATE payment_requests
                SET status='approved', processed_at=NOW()
                WHERE id=$1
                """,
                request_id,
            )
            await conn.execute(
                """
                UPDATE members
                SET is_approved=TRUE, approved_at=NOW()
                WHERE telegram_id=$1
                """,
                payment["telegram_id"],
            )

    try:
        await telegram_app.bot.send_message(
            payment["telegram_id"],
            "✅ 入金確認が完了しました。\n\n"
            "「🚪 グループへ入室する」を押して、本人専用リンクを発行してください。",
            reply_markup=MENU,
        )
    except Exception:
        logger.exception("承認通知に失敗しました")

    return True, "承認しました。"


async def reject_request(request_id: int, reason: str) -> tuple[bool, str]:
    assert pool is not None and telegram_app is not None

    async with pool.acquire() as conn:
        payment = await conn.fetchrow(
            """
            UPDATE payment_requests
            SET status='rejected',
                processed_at=NOW(),
                reject_reason=$2
            WHERE id=$1 AND status='pending'
            RETURNING *
            """,
            request_id,
            reason,
        )

    if not payment:
        return False, "未処理の申請が見つかりません。"

    try:
        await telegram_app.bot.send_message(
            payment["telegram_id"],
            f"❌ 入金確認ができませんでした。\n理由：{reason}",
            reply_markup=MENU,
        )
    except Exception:
        logger.exception("否認通知に失敗しました")

    return True, "否認しました。"


def logged_in(request: Request) -> bool:
    return request.session.get("admin") is True


def page(body: str) -> str:
    return f"""
    <!doctype html>
    <html lang="ja">
    <head>
      <meta charset="utf-8">
      <meta name="viewport" content="width=device-width,initial-scale=1">
      <title>新規入室Bot 管理画面</title>
      <style>
        body{{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;margin:0;background:#f4f6f8;color:#17202a}}
        header{{background:#17202a;color:white;padding:16px 20px;display:flex;justify-content:space-between;align-items:center}}
        main{{max-width:1100px;margin:24px auto;padding:0 16px}}
        .cards{{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:12px;margin-bottom:24px}}
        .card{{background:white;border-radius:14px;padding:16px}}
        .value{{font-size:28px;font-weight:700;margin-top:8px}}
        table{{width:100%;border-collapse:collapse;background:white}}
        th,td{{padding:12px;border-bottom:1px solid #e5e7eb;text-align:left;vertical-align:middle}}
        th{{background:#eef2f5}}
        button,.button{{border:0;border-radius:9px;padding:9px 12px;background:#2563eb;color:white;text-decoration:none;cursor:pointer}}
        .danger{{background:#dc2626}}
        .inline{{display:inline}}
        .scroll{{overflow-x:auto}}
        input{{padding:9px;border:1px solid #ccd3da;border-radius:8px}}
      </style>
    </head>
    <body>
      <header>
        <strong>新規入室Bot 管理画面</strong>
        <a class="button danger" href="/admin/logout">ログアウト</a>
      </header>
      <main>{body}</main>
    </body>
    </html>
    """


@asynccontextmanager
async def lifespan(_: FastAPI):
    global pool, telegram_app

    pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=5)
    await init_db(pool)

    telegram_app = Application.builder().token(BOT_TOKEN).build()
    telegram_app.add_handler(CommandHandler("start", start))
    telegram_app.add_handler(ChatJoinRequestHandler(join_request))
    telegram_app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, text_router)
    )

    await telegram_app.initialize()
    await telegram_app.start()

    if telegram_app.updater is None:
        raise RuntimeError("Telegram updater is unavailable")

    await telegram_app.updater.start_polling(allowed_updates=Update.ALL_TYPES)
    logger.info("新規入室Botと管理画面を起動しました。")

    try:
        yield
    finally:
        if telegram_app.updater:
            await telegram_app.updater.stop()
        await telegram_app.stop()
        await telegram_app.shutdown()
        await pool.close()


app = FastAPI(lifespan=lifespan)
app.add_middleware(
    SessionMiddleware,
    secret_key=SECRET_KEY,
    same_site="lax",
    https_only=True,
)
app.mount("/assets", StaticFiles(directory=BASE_DIR / "assets"), name="assets")


@app.get("/")
async def root():
    return RedirectResponse("/admin", status_code=303)


@app.get("/admin/login", response_class=HTMLResponse)
async def login_page(request: Request):
    if logged_in(request):
        return RedirectResponse("/admin", status_code=303)

    return HTMLResponse(
        """
        <meta charset="utf-8">
        <meta name="viewport" content="width=device-width,initial-scale=1">
        <style>
          body{font-family:-apple-system,sans-serif;background:#f4f6f8;display:grid;place-items:center;min-height:100vh;margin:0}
          form{background:white;padding:24px;border-radius:14px;width:min(360px,90vw)}
          input,button{width:100%;box-sizing:border-box;padding:12px;margin-top:12px}
          button{background:#2563eb;color:white;border:0;border-radius:9px}
        </style>
        <form method="post">
          <h2>管理者ログイン</h2>
          <input type="password" name="password" placeholder="管理パスワード" required>
          <button type="submit">ログイン</button>
        </form>
        """
    )


@app.post("/admin/login")
async def login(request: Request, password: str = Form(...)):
    if password != ADMIN_PASSWORD:
        return HTMLResponse("パスワードが違います。", status_code=401)

    request.session["admin"] = True
    return RedirectResponse("/admin", status_code=303)


@app.get("/admin/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/admin/login", status_code=303)


@app.get("/admin", response_class=HTMLResponse)
async def dashboard(request: Request):
    if not logged_in(request):
        return RedirectResponse("/admin/login", status_code=303)

    assert pool is not None

    async with pool.acquire() as conn:
        stats = await conn.fetchrow(
            """
            SELECT
              (SELECT COUNT(*) FROM members) AS members,
              (SELECT COUNT(*) FROM members WHERE is_approved=TRUE) AS approved,
              (SELECT COUNT(*) FROM payment_requests WHERE status='pending') AS pending
            """
        )

        requests = await conn.fetch(
            """
            SELECT p.*, m.username, m.first_name
            FROM payment_requests p
            JOIN members m ON m.telegram_id=p.telegram_id
            ORDER BY
              CASE p.status WHEN 'pending' THEN 0 ELSE 1 END,
              p.created_at DESC
            LIMIT 100
            """
        )

    rows = []
    for item in requests:
        actions = ""
        if item["status"] == "pending":
            actions = f"""
            <form class="inline" method="post" action="/admin/requests/{item['id']}/approve">
              <button type="submit">承認</button>
            </form>
            <form class="inline" method="post" action="/admin/requests/{item['id']}/reject">
              <input name="reason" placeholder="否認理由" required>
              <button class="danger" type="submit">否認</button>
            </form>
            """

        rows.append(
            f"""
            <tr>
              <td>{item['id']}</td>
              <td>{escape(item['first_name'] or '')}<br>@{escape(item['username'] or 'なし')}</td>
              <td>{item['telegram_id']}</td>
              <td>{yen(item['amount'])}</td>
              <td>{escape(item['status'])}</td>
              <td>{item['created_at'].strftime('%Y-%m-%d %H:%M')}</td>
              <td>{actions}</td>
            </tr>
            """
        )

    body = f"""
    <div class="cards">
      <div class="card">全会員<div class="value">{stats['members']}</div></div>
      <div class="card">承認済み<div class="value">{stats['approved']}</div></div>
      <div class="card">入金確認待ち<div class="value">{stats['pending']}</div></div>
    </div>

    <h2>入金確認申請</h2>
    <div class="scroll">
      <table>
        <thead>
          <tr>
            <th>ID</th><th>会員</th><th>Telegram ID</th>
            <th>金額</th><th>状態</th><th>申請日時</th><th>操作</th>
          </tr>
        </thead>
        <tbody>{''.join(rows)}</tbody>
      </table>
    </div>
    """

    return HTMLResponse(page(body))


@app.post("/admin/requests/{request_id}/approve")
async def admin_approve(request: Request, request_id: int):
    if not logged_in(request):
        return RedirectResponse("/admin/login", status_code=303)

    await approve_request(request_id)
    return RedirectResponse("/admin", status_code=303)


@app.post("/admin/requests/{request_id}/reject")
async def admin_reject(
    request: Request,
    request_id: int,
    reason: str = Form(...),
):
    if not logged_in(request):
        return RedirectResponse("/admin/login", status_code=303)

    await reject_request(request_id, reason)
    return RedirectResponse("/admin", status_code=303)
