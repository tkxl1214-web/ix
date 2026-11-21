"""FastAPI web application for private chat, memory, data review, and training."""

import asyncio
import base64
import hashlib
import hmac
import html
import json
import os
import queue
import re
import secrets
import subprocess
import sys
import threading
import time
import unicodedata
import uuid
from collections import defaultdict, deque
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request as URLRequest, urlopen

import torch
from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from . import database
from .config import BOOTSTRAP_ADMIN_EMAIL, FRONTEND_DIR, GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, GOOGLE_REDIRECT_URI, MAX_TEXT_CHARS, MAX_UPLOAD_BYTES, MAX_RESPONSE_TOKENS, SECRET_KEY
from .model_store import ACTIVE_PATH, active_metadata, get_model, initialize_model, atomic_save
from .security import hash_password, issue_token, read_token, verify_password
from .starter_corpus import STARTER_CORPUS
from .tokenizer import ByteTokenizer


app = FastAPI(title="ixaistudio", docs_url="/api/docs", redoc_url=None)
security = HTTPBearer(auto_error=False)
_limits = defaultdict(deque)
_processes: dict[str, subprocess.Popen] = {}
_limit_lock = threading.Lock()
SYSTEM_USER_EMAIL = "system-learning@local.invalid"


class Credentials(BaseModel):
    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=10, max_length=200)


class RegistrationDetails(BaseModel):
    display_name: str = Field(min_length=1, max_length=80)
    age: int = Field(ge=1, le=120)
    accepted_terms: bool
    accepted_privacy: bool
    accepted_youth: bool


class GoogleRegistration(RegistrationDetails):
    pass


class TextData(BaseModel):
    content: str = Field(min_length=20, max_length=MAX_TEXT_CHARS)
    consent: bool
    category: str | None = None


class MemoryInput(BaseModel):
    content: str = Field(min_length=2, max_length=2000)
    category: str = Field(default="general", max_length=40)


class ChatInput(BaseModel):
    content: str = Field(min_length=1, max_length=5000)
    conversation_id: str | None = None


class FeedbackInput(BaseModel):
    message_id: int
    rating: int = Field(ge=-1, le=1)
    approve_for_training: bool = False


class TrainingInput(BaseModel):
    epochs: int = Field(default=3, ge=1, le=50)


class AutoTrainingInput(BaseModel):
    enabled: bool
    min_new_items: int = Field(default=5, ge=1, le=100)


def normalize_text(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).replace("\x00", " ")
    text = re.sub(r"[\t\r\f\v ]+", " ", text)
    text = re.sub(r"\n\s*\n\s*\n+", "\n\n", text)
    return text.strip()


def classify_text(text: str, requested: str | None = None) -> str:
    categories = {"knowledge", "conversation", "feedback", "general"}
    if requested in categories:
        return requested
    lowered = text.lower()
    if any(term in lowered for term in ("좋아요", "싫어요", "정확하지", "수정해", "feedback", "정답은")):
        return "feedback"
    if "사용자:" in text or "assistant:" in lowered or "ai:" in lowered:
        return "conversation"
    if len(text) > 1000 or any(term in lowered for term in ("정의:", "설명:", "개요:", "매뉴얼", "문서")):
        return "knowledge"
    return "general"


def clean_text(text: str) -> str:
    cleaned = normalize_text(html.unescape(text))
    if len(cleaned) < 20:
        raise HTTPException(422, "학습 텍스트는 정제 후 20자 이상이어야 합니다.")
    if len(cleaned) > MAX_TEXT_CHARS:
        raise HTTPException(413, "학습 텍스트가 허용된 길이를 초과했습니다.")
    return cleaned


def current_user(request: Request, credentials: HTTPAuthorizationCredentials | None = Depends(security)):
    token = credentials.credentials if credentials else request.cookies.get("ai_session")
    if not token:
        raise HTTPException(401, "로그인이 필요합니다.")
    claims = read_token(token)
    if not claims:
        raise HTTPException(401, "로그인 정보가 만료되었거나 올바르지 않습니다.")
    with database.connect() as connection:
        row = connection.execute("SELECT id,email,role,display_name,age FROM users WHERE id=?", (claims.get("sub"),)).fetchone()
    if not row:
        raise HTTPException(401, "사용자 계정을 찾을 수 없습니다.")
    return dict(row)


def admin_user(user=Depends(current_user)):
    if user["role"] != "admin":
        raise HTTPException(403, "관리자 권한이 필요합니다.")
    return user


def ensure_conversation(user_id: int, conversation_id: str | None) -> str:
    with database.transaction() as connection:
        if conversation_id:
            row = connection.execute("SELECT id FROM conversations WHERE id=? AND user_id=?", (conversation_id, user_id)).fetchone()
            if not row:
                raise HTTPException(404, "대화를 찾을 수 없습니다.")
            return conversation_id
        conversation_id = uuid.uuid4().hex
        connection.execute("INSERT INTO conversations(id,user_id,title) VALUES(?,?,?)", (conversation_id, user_id, "새 대화"))
        return conversation_id


def retrieve_memories(user_id: int, query: str, limit: int = 3) -> list[str]:
    terms = {term.lower() for term in re.findall(r"[\w가-힣]{2,}", query)}
    with database.connect() as connection:
        rows = connection.execute("SELECT content,category FROM memories WHERE user_id=? ORDER BY updated_at DESC LIMIT 300", (user_id,)).fetchall()
    ranked = []
    for row in rows:
        memory = row["content"]
        memory_terms = {term.lower() for term in re.findall(r"[\w가-힣]{2,}", memory)}
        score = len(terms & memory_terms) / max(1, len(terms)) if terms else 0
        if score > 0:
            ranked.append((score, memory))
    ranked.sort(key=lambda item: item[0], reverse=True)
    return [memory for _, memory in ranked[:limit]]


def make_prompt(user_id: int, conversation_id: str, user_text: str) -> str:
    with database.connect() as connection:
        rows = connection.execute(
            "SELECT role,content FROM messages WHERE conversation_id=? AND user_id=? ORDER BY id DESC LIMIT 7",
            (conversation_id, user_id),
        ).fetchall()
    rows = list(reversed(rows))
    memories = retrieve_memories(user_id, user_text)
    parts = []
    if memories:
        parts.append("기억한 정보:\n" + "\n".join("- " + item for item in memories))
    if rows:
        parts.append("최근 대화:\n" + "\n".join(("사용자" if r["role"] == "user" else "AI") + ": " + r["content"] for r in rows))
    parts.append("AI:")
    return "\n".join(parts)[-700:]


def produce_tokens(prompt: str, output_queue: queue.Queue, max_tokens: int = MAX_RESPONSE_TOKENS):
    try:
        model, device, config = get_model()
        tokenizer = ByteTokenizer()
        token_ids = tokenizer.encode(prompt)
        context_limit = max(2, config["block_size"])
        token_ids = token_ids[-context_limit:]
        context = torch.tensor([token_ids], dtype=torch.long, device=device)
        for _ in range(max_tokens):
            token = model.generate_one(context, temperature=0.85, top_k=40)
            if token == tokenizer.eos_id:
                break
            if token == tokenizer.bos_id:
                continue
            context = torch.cat((context, torch.tensor([[token]], dtype=torch.long, device=device)), dim=1)
            if context.shape[1] > config["block_size"]:
                context = context[:, -config["block_size"]:]
            output_queue.put(token)
    except Exception as error:
        output_queue.put({"error": f"{type(error).__name__}: {error}"})
    finally:
        output_queue.put(None)


def launch_worker(job_id: str, resume: bool = False):
    previous = _processes.get(job_id)
    if previous and previous.poll() is None:
        previous.wait(timeout=10)
    args = [sys.executable, "-m", "backend.worker", "--job-id", job_id]
    if resume:
        args.append("--resume")
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    process = subprocess.Popen(args, cwd=Path(__file__).resolve().parents[1], creationflags=flags,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    _processes[job_id] = process
    with database.transaction() as connection:
        connection.execute("UPDATE training_jobs SET worker_pid=? WHERE id=?", (process.pid, job_id))
    return process.pid


def reconcile_interrupted_jobs():
    """Expose a safe resume action for work whose OS process ended with the web server."""
    import psutil
    with database.connect() as connection:
        rows = connection.execute("SELECT id,status,worker_pid FROM training_jobs WHERE status IN ('queued','running')").fetchall()
    for row in rows:
        alive = False
        if row["worker_pid"]:
            try:
                process = psutil.Process(row["worker_pid"])
                command = " ".join(process.cmdline())
                alive = process.is_running() and "backend.worker" in command and row["id"] in command
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                alive = False
        if not alive:
            with database.transaction() as connection:
                connection.execute(
                    "UPDATE training_jobs SET status='paused',pause_requested=0,cancel_requested=0,error='서버가 재시작되어 저장된 체크포인트에서 재개할 수 있습니다.' WHERE id=? AND status IN ('queued','running')",
                    (row["id"],),
                )


def setting_value(connection, key: str, default: str):
    row = connection.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def seed_starter_corpus(admin_id: int) -> int:
    """Install original project-authored material once and enable first training."""
    inserted = 0
    with database.transaction() as connection:
        for title, content in STARTER_CORPUS:
            normalized = normalize_text(f"사용자: {title}에 대해 간단히 설명해 주세요.\nAI: {content}")
            digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
            if connection.execute("SELECT 1 FROM datasets WHERE content_hash=?", (digest,)).fetchone():
                continue
            connection.execute(
                "INSERT INTO datasets(owner_id,content,category,source,content_hash,consent,status) VALUES(?,?,? ,?,?,1,'approved')",
                (admin_id, normalized, "knowledge", f"내장 자료 · {title}", digest),
            )
            inserted += 1
        if inserted:
            connection.execute("UPDATE settings SET value='1',updated_at=CURRENT_TIMESTAMP WHERE key='auto_training_enabled'")
            connection.execute("UPDATE settings SET value='1',updated_at=CURRENT_TIMESTAMP WHERE key='auto_training_min_items'")
    return inserted


def ensure_system_learning_owner() -> int:
    """Create a non-login service owner so starter learning begins before sign-up."""
    with database.transaction() as connection:
        row = connection.execute("SELECT id FROM users WHERE email=?", (SYSTEM_USER_EMAIL,)).fetchone()
        if row:
            return int(row["id"])
        cursor = connection.execute(
            "INSERT INTO users(email,password_hash,role) VALUES(?,?, 'user')",
            (SYSTEM_USER_EMAIL, hash_password(secrets.token_urlsafe(48))),
        )
        return int(cursor.lastrowid)


def google_oauth_enabled() -> bool:
    return bool(GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET and GOOGLE_REDIRECT_URI)


def google_oauth_secure_cookie() -> bool:
    return GOOGLE_REDIRECT_URI.lower().startswith("https://")


def encode_urlsafe(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def sign_google_state(state: str, verifier: str) -> str:
    payload = encode_urlsafe(json.dumps(
        {"state": state, "verifier": verifier, "issued_at": int(time.time())},
        separators=(",", ":"),
    ).encode("utf-8"))
    signature = encode_urlsafe(hmac.new(SECRET_KEY.encode(), ("google-oauth:" + payload).encode(), hashlib.sha256).digest())
    return payload + "." + signature


def verify_google_state(cookie: str | None, supplied_state: str | None) -> dict | None:
    if not cookie or not supplied_state or "." not in cookie:
        return None
    payload, supplied_signature = cookie.rsplit(".", 1)
    expected = encode_urlsafe(hmac.new(SECRET_KEY.encode(), ("google-oauth:" + payload).encode(), hashlib.sha256).digest())
    if not hmac.compare_digest(supplied_signature, expected):
        return None
    try:
        raw = base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4))
        claims = json.loads(raw)
        age = int(time.time()) - int(claims["issued_at"])
        if age < 0 or age > 600 or not hmac.compare_digest(str(claims["state"]), supplied_state):
            return None
        if not isinstance(claims.get("verifier"), str) or len(claims["verifier"]) < 43:
            return None
        return claims
    except (ValueError, KeyError, TypeError, json.JSONDecodeError):
        return None


def google_signup_ticket_hash(ticket: str) -> str:
    return hashlib.sha256(ticket.encode("utf-8")).hexdigest()


def pending_google_registration(request: Request):
    ticket = request.cookies.get("google_signup_ticket")
    if not ticket:
        raise HTTPException(401, "Google 가입 단계가 만료됐습니다. Google 로그인을 다시 시작해 주세요.")
    with database.connect() as connection:
        pending = connection.execute(
            """SELECT nonce_hash,email,google_sub,display_name,existing_user_id
               FROM oauth_registrations
               WHERE nonce_hash=? AND created_at>=datetime('now','-10 minutes')""",
            (google_signup_ticket_hash(ticket),),
        ).fetchone()
    if not pending:
        raise HTTPException(401, "Google 가입 단계가 만료됐습니다. Google 로그인을 다시 시작해 주세요.")
    return dict(pending)


def exchange_google_code(code: str, verifier: str) -> dict:
    form = urlencode({
        "code": code,
        "client_id": GOOGLE_CLIENT_ID,
        "client_secret": GOOGLE_CLIENT_SECRET,
        "redirect_uri": GOOGLE_REDIRECT_URI,
        "grant_type": "authorization_code",
        "code_verifier": verifier,
    }).encode("ascii")
    token_request = URLRequest(
        "https://oauth2.googleapis.com/token", data=form,
        headers={"Content-Type": "application/x-www-form-urlencoded"}, method="POST",
    )
    try:
        with urlopen(token_request, timeout=12) as response:
            tokens = json.loads(response.read().decode("utf-8"))
        access_token = tokens.get("access_token")
        if not isinstance(access_token, str) or not access_token:
            raise ValueError("Google did not return an access token.")
        user_request = URLRequest(
            "https://openidconnect.googleapis.com/v1/userinfo",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        with urlopen(user_request, timeout=12) as response:
            profile = json.loads(response.read().decode("utf-8"))
        if not profile.get("email_verified") or not profile.get("sub") or not profile.get("email"):
            raise ValueError("Google did not return a verified email address.")
        return profile
    except (HTTPError, URLError, TimeoutError, ValueError, json.JSONDecodeError) as error:
        raise HTTPException(502, "Google 계정 확인에 실패했습니다. 잠시 후 다시 시도해 주세요.") from error


def auto_training_loop():
    interval = max(5, int(os.getenv("AUTO_TRAINING_CHECK_SECONDS", "10")))
    while not _auto_stop.wait(interval):
        try:
            with database.connect() as connection:
                enabled = setting_value(connection, "auto_training_enabled", "1") == "1"
                threshold = int(setting_value(connection, "auto_training_min_items", "1"))
                busy = connection.execute("SELECT 1 FROM training_jobs WHERE status IN ('queued','running','paused') LIMIT 1").fetchone()
                admin = connection.execute("SELECT id FROM users WHERE role='admin' ORDER BY id LIMIT 1").fetchone()
                owner = admin or connection.execute("SELECT id FROM users WHERE email<>? ORDER BY id LIMIT 1", (SYSTEM_USER_EMAIL,)).fetchone()
                approved = connection.execute("SELECT id FROM datasets WHERE status='approved' AND consent=1 ORDER BY id").fetchall()
                latest = connection.execute("SELECT dataset_ids FROM training_jobs WHERE status='completed' ORDER BY finished_at DESC LIMIT 1").fetchone()
            if not enabled or busy or not owner or not approved:
                continue
            trained_ids = set(json.loads(latest["dataset_ids"])) if latest else set()
            current_ids = [row["id"] for row in approved]
            if len(set(current_ids) - trained_ids) < threshold:
                continue
            job_id = uuid.uuid4().hex
            with database.transaction() as connection:
                connection.execute(
                    "INSERT INTO training_jobs(id,owner_id,status,total_epochs,dataset_ids,example_count) VALUES(?,?, 'queued',3,?,?)",
                    (job_id, owner["id"], json.dumps(current_ids), len(current_ids)),
                )
            launch_worker(job_id)
        except Exception as error:
            print(f"automatic training scheduler: {type(error).__name__}: {error}")


_auto_stop = threading.Event()
_auto_thread: threading.Thread | None = None


def parse_upload(name: str, raw: bytes) -> str:
    suffix = Path(name or "").suffix.lower()
    if suffix in {".txt", ".md", ".csv", ".json"}:
        return raw.decode("utf-8-sig")
    if suffix == ".pdf":
        from pypdf import PdfReader
        import io
        reader = PdfReader(io.BytesIO(raw), strict=True)
        if len(reader.pages) > 200:
            raise HTTPException(413, "PDF는 200페이지 이하만 처리할 수 있습니다.")
        return "\n".join(page.extract_text() or "" for page in reader.pages)
    if suffix == ".docx":
        from docx import Document
        import io
        document = Document(io.BytesIO(raw))
        return "\n".join(paragraph.text for paragraph in document.paragraphs)
    raise HTTPException(415, "지원 파일 형식: TXT, Markdown, CSV, JSON, PDF, DOCX")


@app.middleware("http")
async def rate_limit(request: Request, call_next):
    if request.url.path.startswith("/api/"):
        key = request.client.host if request.client else "unknown"
        now = time.monotonic()
        with _limit_lock:
            bucket = _limits[key]
            while bucket and bucket[0] < now - 60:
                bucket.popleft()
            if len(bucket) >= 180:
                return JSONResponse({"detail": "요청이 너무 많습니다. 잠시 후 다시 시도해 주세요."}, status_code=429)
            bucket.append(now)
    return await call_next(request)


@app.on_event("startup")
def startup():
    global _auto_thread
    database.initialize()
    with database.connect() as connection:
        admin = connection.execute("SELECT id FROM users WHERE role='admin' ORDER BY id LIMIT 1").fetchone()
    system_owner_id = ensure_system_learning_owner()
    seed_starter_corpus(admin["id"] if admin else system_owner_id)
    torch.set_num_threads(max(1, min(4, os.cpu_count() or 1)))
    initialize_model()
    reconcile_interrupted_jobs()
    _auto_stop.clear()
    _auto_thread = threading.Thread(target=auto_training_loop, name="auto-training-scheduler", daemon=True)
    _auto_thread.start()


@app.on_event("shutdown")
def shutdown():
    _auto_stop.set()


@app.get("/")
def home():
    return FileResponse(FRONTEND_DIR / "index.html")


@app.get("/privacy")
def privacy_policy():
    return FileResponse(FRONTEND_DIR / "privacy.html")


@app.get("/terms")
def terms_of_use():
    return FileResponse(FRONTEND_DIR / "terms.html")


@app.get("/youth")
def youth_policy():
    return FileResponse(FRONTEND_DIR / "youth.html")


@app.get("/assets/{name}")
def assets(name: str):
    if name not in {"app.js", "styles.css", "experience.css", "ixaistudio-logo.svg"}:
        raise HTTPException(404, "파일을 찾을 수 없습니다.")
    return FileResponse(FRONTEND_DIR / name)


@app.get("/api/health")
def health():
    return {"status": "ok", "ai_api": False}


@app.post("/api/auth/register")
def email_registration_disabled():
    raise HTTPException(410, "계정 가입은 Google 로그인을 사용해 주세요.")


@app.post("/api/auth/login")
def login(payload: Credentials):
    email = payload.email.strip().lower()
    if email == SYSTEM_USER_EMAIL:
        raise HTTPException(401, "이메일 또는 비밀번호가 올바르지 않습니다.")
    with database.connect() as connection:
        row = connection.execute("SELECT id,email,role,display_name,age,password_hash FROM users WHERE email=?", (email,)).fetchone()
    if not row or not verify_password(payload.password, row["password_hash"]):
        raise HTTPException(401, "이메일 또는 비밀번호가 올바르지 않습니다.")
    return {"token": issue_token(row["id"], row["role"]), "user": {"id": row["id"], "email": row["email"], "role": row["role"], "display_name": row["display_name"], "age": row["age"]}}


@app.get("/api/auth/google/status")
def google_login_status():
    return {"enabled": google_oauth_enabled()}


@app.get("/api/auth/google/registration/status")
def google_registration_status(request: Request):
    pending = pending_google_registration(request)
    return {"email": pending["email"], "display_name": pending["display_name"]}


@app.post("/api/auth/google/registration/cancel")
def cancel_google_registration(request: Request, response: Response):
    ticket = request.cookies.get("google_signup_ticket")
    if ticket:
        with database.transaction() as connection:
            connection.execute("DELETE FROM oauth_registrations WHERE nonce_hash=?", (google_signup_ticket_hash(ticket),))
    response.delete_cookie("google_signup_ticket", path="/", secure=google_oauth_secure_cookie(), httponly=True, samesite="lax")
    return {"cancelled": True}


@app.post("/api/auth/google/register")
def register_google_account(payload: GoogleRegistration, request: Request, response: Response):
    pending = pending_google_registration(request)
    display_name = normalize_text(payload.display_name)
    if not display_name:
        raise HTTPException(422, "이름을 입력해 주세요.")
    if not (payload.accepted_terms and payload.accepted_privacy and payload.accepted_youth):
        raise HTTPException(422, "가입하려면 세 가지 이용 정책에 모두 동의해야 합니다.")
    password_hash = hash_password(secrets.token_urlsafe(48))
    try:
        with database.transaction() as connection:
            if pending["existing_user_id"]:
                existing = connection.execute(
                    "SELECT id,email,role,google_sub FROM users WHERE id=?",
                    (pending["existing_user_id"],),
                ).fetchone()
                if not existing or existing["email"] != pending["email"]:
                    raise HTTPException(409, "연결할 계정을 찾을 수 없습니다. Google 로그인을 다시 시작해 주세요.")
                if existing["google_sub"] and existing["google_sub"] != pending["google_sub"]:
                    raise HTTPException(409, "이 계정에는 다른 Google 계정이 연결되어 있습니다.")
                connection.execute(
                    """UPDATE users SET google_sub=?,display_name=?,age=?,accepted_terms_at=CURRENT_TIMESTAMP,
                       accepted_privacy_at=CURRENT_TIMESTAMP,accepted_youth_at=CURRENT_TIMESTAMP,policy_version='2026-11-21'
                       WHERE id=?""",
                    (pending["google_sub"], display_name, payload.age, existing["id"]),
                )
                user = {"id": existing["id"], "email": existing["email"], "role": existing["role"], "display_name": display_name, "age": payload.age}
            else:
                existing = connection.execute("SELECT id FROM users WHERE email=? OR google_sub=?", (pending["email"], pending["google_sub"])).fetchone()
                if existing:
                    raise HTTPException(409, "이미 연결된 계정입니다. Google 로그인을 다시 시도해 주세요.")
                user_count = connection.execute("SELECT COUNT(*) FROM users WHERE email<>?", (SYSTEM_USER_EMAIL,)).fetchone()[0]
                role = "admin" if pending["email"] == BOOTSTRAP_ADMIN_EMAIL or (user_count == 0 and not BOOTSTRAP_ADMIN_EMAIL) else "user"
                cursor = connection.execute(
                    """INSERT INTO users(email,password_hash,google_sub,display_name,age,accepted_terms_at,accepted_privacy_at,accepted_youth_at,policy_version,role)
                       VALUES(?,?,?,?,?,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP,'2026-11-21',?)""",
                    (pending["email"], password_hash, pending["google_sub"], display_name, payload.age, role),
                )
                user = {"id": cursor.lastrowid, "email": pending["email"], "role": role, "display_name": display_name, "age": payload.age}
            connection.execute("DELETE FROM oauth_registrations WHERE nonce_hash=?", (pending["nonce_hash"],))
    except HTTPException:
        raise
    except Exception as error:
        if "UNIQUE" in str(error):
            raise HTTPException(409, "이미 등록된 계정입니다. Google 로그인을 다시 시도해 주세요.")
        raise
    response.delete_cookie("google_signup_ticket", path="/", secure=google_oauth_secure_cookie(), httponly=True, samesite="lax")
    seed_starter_corpus(user["id"])
    return {"token": issue_token(user["id"], user["role"]), "user": user}


@app.get("/api/auth/google")
def google_login():
    if not google_oauth_enabled():
        raise HTTPException(503, "Google OAuth 클라이언트 설정이 필요합니다.")
    state = secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(48)
    challenge = encode_urlsafe(hashlib.sha256(verifier.encode("ascii")).digest())
    params = urlencode({
        "client_id": GOOGLE_CLIENT_ID,
        "redirect_uri": GOOGLE_REDIRECT_URI,
        "response_type": "code",
        "scope": "openid email profile",
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "prompt": "select_account",
    })
    response = RedirectResponse("https://accounts.google.com/o/oauth2/v2/auth?" + params, status_code=302)
    response.set_cookie(
        "google_oauth_state", sign_google_state(state, verifier), max_age=600,
        httponly=True, secure=google_oauth_secure_cookie(), samesite="lax", path="/",
    )
    return response


@app.get("/api/auth/google/callback")
def google_login_callback(request: Request, code: str | None = None, state: str | None = None, error: str | None = None):
    response = RedirectResponse("/?auth_error=google", status_code=303)
    response.delete_cookie("google_oauth_state", path="/", secure=google_oauth_secure_cookie(), httponly=True, samesite="lax")
    if error or not google_oauth_enabled():
        return response
    oauth_state = verify_google_state(request.cookies.get("google_oauth_state"), state)
    if not oauth_state or not code:
        return RedirectResponse("/?auth_error=google_state", status_code=303)
    profile = exchange_google_code(code, oauth_state["verifier"])
    email = str(profile["email"]).strip().lower()
    subject = str(profile["sub"])
    if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email):
        return response
    signup = None
    user = None
    try:
        with database.transaction() as connection:
            user = connection.execute(
                """SELECT id,email,role,display_name,age,google_sub,accepted_terms_at,accepted_privacy_at,accepted_youth_at
                   FROM users WHERE google_sub=?""",
                (subject,),
            ).fetchone()
            if user is not None and not all(user[field] for field in ("accepted_terms_at", "accepted_privacy_at", "accepted_youth_at")):
                signup = {"email": email, "google_sub": subject, "display_name": str(profile.get("name") or user["display_name"] or "")[:80], "existing_user_id": user["id"]}
                user = None
            if user is None and signup is None:
                if email == SYSTEM_USER_EMAIL:
                    return response
                existing = connection.execute(
                    """SELECT id,email,role,display_name,age,google_sub,accepted_terms_at,accepted_privacy_at,accepted_youth_at
                       FROM users WHERE email=?""",
                    (email,),
                ).fetchone()
                if existing:
                    if existing["google_sub"] and existing["google_sub"] != subject:
                        return response
                    if all(existing[field] for field in ("accepted_terms_at", "accepted_privacy_at", "accepted_youth_at")):
                        connection.execute("UPDATE users SET google_sub=? WHERE id=?", (subject, existing["id"]))
                        user = {"id": existing["id"], "email": existing["email"], "role": existing["role"], "display_name": existing["display_name"], "age": existing["age"], "google_sub": subject}
                    else:
                        signup = {"email": email, "google_sub": subject, "display_name": str(profile.get("name") or "")[:80], "existing_user_id": existing["id"]}
                else:
                    signup = {"email": email, "google_sub": subject, "display_name": str(profile.get("name") or "")[:80], "existing_user_id": None}
    except Exception:
        return response
    if signup:
        ticket = secrets.token_urlsafe(32)
        with database.transaction() as connection:
            connection.execute("DELETE FROM oauth_registrations WHERE created_at<datetime('now','-1 day')")
            connection.execute(
                "INSERT INTO oauth_registrations(nonce_hash,email,google_sub,display_name,existing_user_id) VALUES(?,?,?,?,?)",
                (google_signup_ticket_hash(ticket), signup["email"], signup["google_sub"], signup["display_name"], signup["existing_user_id"]),
            )
        signup_response = RedirectResponse("/?google_signup=1", status_code=303)
        signup_response.set_cookie(
            "google_signup_ticket", ticket, max_age=600, httponly=True,
            secure=google_oauth_secure_cookie(), samesite="lax", path="/",
        )
        signup_response.delete_cookie("google_oauth_state", path="/", secure=google_oauth_secure_cookie(), httponly=True, samesite="lax")
        return signup_response
    seed_starter_corpus(user["id"])
    response = RedirectResponse("/", status_code=303)
    response.set_cookie(
        "ai_session", issue_token(user["id"], user["role"]), max_age=8 * 60 * 60,
        httponly=True, secure=google_oauth_secure_cookie(), samesite="lax", path="/",
    )
    response.delete_cookie("google_oauth_state", path="/", secure=google_oauth_secure_cookie(), httponly=True, samesite="lax")
    return response


@app.post("/api/auth/logout")
def logout(response: Response):
    response.delete_cookie("ai_session", path="/", secure=google_oauth_secure_cookie(), httponly=True, samesite="lax")
    return {"logged_out": True}


@app.get("/api/auth/me")
def me(user=Depends(current_user)):
    return user


@app.delete("/api/account")
def delete_account(user=Depends(current_user)):
    with database.transaction() as connection:
        dataset_ids = {row["id"] for row in connection.execute("SELECT id FROM datasets WHERE owner_id=?", (user["id"],)).fetchall()}
        ensure_not_training(connection, dataset_ids)
        connection.execute("DELETE FROM users WHERE id=?", (user["id"],))
    return {"deleted": True}


@app.get("/api/conversations")
def list_conversations(user=Depends(current_user)):
    with database.connect() as connection:
        rows = connection.execute(
            "SELECT c.id,c.title,c.created_at,c.updated_at,(SELECT content FROM messages m WHERE m.conversation_id=c.id ORDER BY m.id DESC LIMIT 1) AS preview FROM conversations c WHERE c.user_id=? ORDER BY c.updated_at DESC",
            (user["id"],),
        ).fetchall()
    return [dict(row) for row in rows]


@app.post("/api/conversations")
def new_conversation(user=Depends(current_user)):
    identifier = ensure_conversation(user["id"], None)
    return {"id": identifier, "title": "새 대화"}


@app.get("/api/conversations/{conversation_id}/messages")
def messages(conversation_id: str, user=Depends(current_user)):
    with database.connect() as connection:
        owned = connection.execute("SELECT 1 FROM conversations WHERE id=? AND user_id=?", (conversation_id, user["id"])).fetchone()
        if not owned:
            raise HTTPException(404, "대화를 찾을 수 없습니다.")
        rows = connection.execute("SELECT id,role,content,feedback,created_at FROM messages WHERE conversation_id=? ORDER BY id", (conversation_id,)).fetchall()
    return [dict(row) for row in rows]


@app.delete("/api/conversations/{conversation_id}")
def delete_conversation(conversation_id: str, user=Depends(current_user)):
    with database.transaction() as connection:
        cursor = connection.execute("DELETE FROM conversations WHERE id=? AND user_id=?", (conversation_id, user["id"]))
        if not cursor.rowcount:
            raise HTTPException(404, "대화를 찾을 수 없습니다.")
    return {"deleted": True}


@app.post("/api/chat")
async def chat(payload: ChatInput, user=Depends(current_user)):
    conversation_id = ensure_conversation(user["id"], payload.conversation_id)
    user_text = normalize_text(payload.content)
    if not user_text:
        raise HTTPException(422, "메시지를 입력해 주세요.")
    with database.transaction() as connection:
        existing_count = connection.execute("SELECT COUNT(*) FROM messages WHERE conversation_id=?", (conversation_id,)).fetchone()[0]
        connection.execute("INSERT INTO messages(conversation_id,user_id,role,content) VALUES(?,?,?,?)", (conversation_id, user["id"], "user", user_text))
        if existing_count == 0:
            title = user_text.replace("\n", " ")[:48]
            connection.execute("UPDATE conversations SET title=?,updated_at=CURRENT_TIMESTAMP WHERE id=?", (title, conversation_id))
        else:
            connection.execute("UPDATE conversations SET updated_at=CURRENT_TIMESTAMP WHERE id=?", (conversation_id,))
    prompt = make_prompt(user["id"], conversation_id, user_text)
    output = queue.Queue()
    producer = threading.Thread(target=produce_tokens, args=(prompt, output), daemon=True)
    producer.start()

    async def event_stream():
        yield "event: meta\ndata: " + json.dumps({"conversation_id": conversation_id}) + "\n\n"
        decoder = ByteTokenizer().stream_decoder()
        answer_parts = []
        while True:
            token = await run_in_threadpool(output.get)
            if token is None:
                break
            if isinstance(token, dict) and "error" in token:
                yield "event: error\ndata: " + json.dumps({"detail": token["error"]}, ensure_ascii=False) + "\n\n"
                return
            text = decoder.decode(bytes([token]), final=False)
            if text:
                answer_parts.append(text)
                yield "event: token\ndata: " + json.dumps({"text": text}, ensure_ascii=False) + "\n\n"
        final_text = decoder.decode(b"", final=True)
        if final_text:
            answer_parts.append(final_text)
            yield "event: token\ndata: " + json.dumps({"text": final_text}, ensure_ascii=False) + "\n\n"
        answer = "".join(answer_parts).strip()
        if answer:
            with database.transaction() as connection:
                connection.execute("INSERT INTO messages(conversation_id,user_id,role,content) VALUES(?,?,?,?)", (conversation_id, user["id"], "assistant", answer))
                connection.execute("UPDATE conversations SET updated_at=CURRENT_TIMESTAMP WHERE id=?", (conversation_id,))
        yield "event: done\ndata: {}\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.post("/api/feedback")
def feedback(payload: FeedbackInput, user=Depends(current_user)):
    with database.transaction() as connection:
        row = connection.execute("SELECT m.id,m.content,m.role,m.conversation_id FROM messages m WHERE m.id=? AND m.user_id=?", (payload.message_id, user["id"])).fetchone()
        if not row or row["role"] != "assistant":
            raise HTTPException(404, "답변을 찾을 수 없습니다.")
        connection.execute("UPDATE messages SET feedback=? WHERE id=?", (payload.rating, payload.message_id))
        if payload.approve_for_training and payload.rating != 0:
            question = connection.execute(
                "SELECT content FROM messages WHERE conversation_id=? AND role='user' AND id<? ORDER BY id DESC LIMIT 1",
                (row["conversation_id"], payload.message_id),
            ).fetchone()
            if question:
                content = normalize_text(f"사용자: {question['content']}\nAI: {row['content']}")
                digest = hashlib.sha256(content.encode()).hexdigest()
                connection.execute(
                    "INSERT OR IGNORE INTO datasets(owner_id,content,category,source,content_hash,consent,status) VALUES(?,?,?,?,?,1,'pending')",
                    (user["id"], content, "feedback", "approved_feedback", digest),
                )
    return {"saved": True, "submitted_for_review": payload.approve_for_training and payload.rating != 0}


@app.get("/api/memories")
def list_memories(user=Depends(current_user)):
    with database.connect() as connection:
        rows = connection.execute("SELECT id,content,category,created_at,updated_at FROM memories WHERE user_id=? ORDER BY updated_at DESC", (user["id"],)).fetchall()
    return [dict(row) for row in rows]


@app.post("/api/memories")
def add_memory(payload: MemoryInput, user=Depends(current_user)):
    content = normalize_text(payload.content)
    with database.transaction() as connection:
        cursor = connection.execute("INSERT INTO memories(user_id,content,category) VALUES(?,?,?)", (user["id"], content, payload.category[:40]))
    return {"id": cursor.lastrowid, "content": content, "category": payload.category}


@app.put("/api/memories/{memory_id}")
def edit_memory(memory_id: int, payload: MemoryInput, user=Depends(current_user)):
    with database.transaction() as connection:
        cursor = connection.execute("UPDATE memories SET content=?,category=?,updated_at=CURRENT_TIMESTAMP WHERE id=? AND user_id=?",
                                    (normalize_text(payload.content), payload.category[:40], memory_id, user["id"]))
        if not cursor.rowcount:
            raise HTTPException(404, "기억을 찾을 수 없습니다.")
    return {"updated": True}


@app.delete("/api/memories/{memory_id}")
def delete_memory(memory_id: int, user=Depends(current_user)):
    with database.transaction() as connection:
        cursor = connection.execute("DELETE FROM memories WHERE id=? AND user_id=?", (memory_id, user["id"]))
        if not cursor.rowcount:
            raise HTTPException(404, "기억을 찾을 수 없습니다.")
    return {"deleted": True}


def dataset_record(connection, user_id: int, text: str, consent: bool, source: str, category: str | None):
    if not consent:
        raise HTTPException(422, "학습에 사용하려면 명시적인 동의가 필요합니다.")
    content = clean_text(text)
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
    if connection.execute("SELECT 1 FROM datasets WHERE content_hash=?", (digest,)).fetchone():
        raise HTTPException(409, "같은 내용이 이미 등록되어 있습니다.")
    cursor = connection.execute(
        "INSERT INTO datasets(owner_id,content,category,source,content_hash,consent,status) VALUES(?,?,?,?,?,1,'pending')",
        (user_id, content, classify_text(content, category), source, digest),
    )
    return cursor.lastrowid


def ensure_not_training(connection, dataset_ids: set[int]):
    if not dataset_ids:
        return
    rows = connection.execute("SELECT dataset_ids FROM training_jobs WHERE status IN ('queued','running','paused')").fetchall()
    for row in rows:
        if dataset_ids.intersection(json.loads(row["dataset_ids"])):
            raise HTTPException(409, "이 데이터가 학습 작업에 사용 중이거나 일시 중단되어 있습니다. 작업을 취소한 뒤 다시 시도해 주세요.")


@app.post("/api/datasets")
def add_dataset(payload: TextData, user=Depends(current_user)):
    with database.transaction() as connection:
        dataset_id = dataset_record(connection, user["id"], payload.content, payload.consent, "text", payload.category)
    return {"id": dataset_id, "status": "pending", "message": "동의한 데이터가 관리자 검토 대기열에 등록됐습니다."}


@app.post("/api/datasets/upload")
async def upload_dataset(file: UploadFile = File(...), consent: bool = Form(...), user=Depends(current_user)):
    raw = await file.read(MAX_UPLOAD_BYTES + 1)
    if len(raw) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, "파일은 5MB 이하만 업로드할 수 있습니다.")
    if not raw:
        raise HTTPException(422, "빈 파일은 등록할 수 없습니다.")
    try:
        text = await run_in_threadpool(parse_upload, file.filename or "", raw)
    except UnicodeDecodeError:
        raise HTTPException(422, "UTF-8 텍스트 파일만 처리할 수 있습니다.")
    except Exception as error:
        if isinstance(error, HTTPException):
            raise error
        raise HTTPException(422, f"문서에서 텍스트를 읽지 못했습니다: {type(error).__name__}")
    with database.transaction() as connection:
        dataset_id = dataset_record(connection, user["id"], text, consent, "upload:" + Path(file.filename or "document").suffix.lower(), None)
    return {"id": dataset_id, "status": "pending", "message": "문서에서 추출한 텍스트가 검토 대기열에 등록됐습니다."}


@app.get("/api/datasets")
def list_datasets(user=Depends(current_user)):
    with database.connect() as connection:
        if user["role"] == "admin":
            rows = connection.execute("SELECT id,owner_id,category,source,consent,status,length(content) AS characters,created_at FROM datasets ORDER BY created_at DESC LIMIT 500").fetchall()
        else:
            rows = connection.execute("SELECT id,category,source,consent,status,length(content) AS characters,created_at FROM datasets WHERE owner_id=? ORDER BY created_at DESC", (user["id"],)).fetchall()
    return [dict(row) for row in rows]


@app.get("/api/datasets/{dataset_id}")
def get_dataset(dataset_id: int, user=Depends(current_user)):
    with database.connect() as connection:
        if user["role"] == "admin":
            row = connection.execute("SELECT id,owner_id,content,category,source,consent,status,created_at FROM datasets WHERE id=?", (dataset_id,)).fetchone()
        else:
            row = connection.execute("SELECT id,content,category,source,consent,status,created_at FROM datasets WHERE id=? AND owner_id=?", (dataset_id,user["id"])).fetchone()
    if not row:
        raise HTTPException(404, "학습 데이터를 찾을 수 없습니다.")
    return dict(row)


@app.delete("/api/datasets/{dataset_id}")
def delete_dataset(dataset_id: int, user=Depends(current_user)):
    with database.transaction() as connection:
        sql = "SELECT id FROM datasets WHERE id=?" + ("" if user["role"] == "admin" else " AND owner_id=?")
        parameters = (dataset_id,) if user["role"] == "admin" else (dataset_id, user["id"])
        if not connection.execute(sql, parameters).fetchone():
            raise HTTPException(404, "학습 데이터를 찾을 수 없습니다.")
        ensure_not_training(connection, {dataset_id})
        if user["role"] == "admin":
            cursor = connection.execute("DELETE FROM datasets WHERE id=?", (dataset_id,))
        else:
            cursor = connection.execute("DELETE FROM datasets WHERE id=? AND owner_id=?", (dataset_id, user["id"]))
        if not cursor.rowcount:
            raise HTTPException(404, "학습 데이터를 찾을 수 없습니다.")
    return {"deleted": True}


@app.post("/api/datasets/{dataset_id}/review")
def review_dataset(dataset_id: int, decision: str, user=Depends(admin_user)):
    if decision not in {"approve", "reject"}:
        raise HTTPException(422, "decision은 approve 또는 reject여야 합니다.")
    status = "approved" if decision == "approve" else "rejected"
    with database.transaction() as connection:
        cursor = connection.execute("UPDATE datasets SET status=?,reviewed_by=?,reviewed_at=CURRENT_TIMESTAMP WHERE id=? AND consent=1 AND status='pending'",
                                    (status,user["id"],dataset_id))
        if not cursor.rowcount:
            raise HTTPException(404, "동의한 대기 데이터를 찾을 수 없습니다.")
    return {"id": dataset_id, "status": status}


@app.get("/api/dashboard")
def dashboard(user=Depends(current_user)):
    with database.connect() as connection:
        approved = connection.execute("SELECT COUNT(*) FROM datasets WHERE status='approved' AND consent=1").fetchone()[0]
        pending = connection.execute("SELECT COUNT(*) FROM datasets WHERE status='pending' AND consent=1").fetchone()[0]
        jobs = connection.execute("SELECT COUNT(*) FROM training_jobs WHERE status='completed'").fetchone()[0]
        recent_jobs = connection.execute("SELECT id,status,progress,epoch,total_epochs,training_loss,validation_loss,baseline_loss,validation_accuracy,error,created_at,finished_at FROM training_jobs ORDER BY created_at DESC LIMIT 8").fetchall()
        versions = connection.execute("SELECT version,validation_loss,active,created_at FROM model_versions ORDER BY created_at DESC").fetchall()
        metrics = connection.execute("SELECT tm.epoch,tm.training_loss,tm.validation_loss,tm.validation_accuracy,tm.created_at FROM training_metrics tm ORDER BY tm.id DESC LIMIT 20").fetchall()
        material_job = connection.execute(
            "SELECT dataset_ids,status FROM training_jobs WHERE status IN ('running','completed') ORDER BY CASE status WHEN 'running' THEN 0 ELSE 1 END, started_at DESC, created_at DESC LIMIT 1"
        ).fetchone()
        material_ids = []
        if material_job:
            try:
                material_ids = [int(item) for item in json.loads(material_job["dataset_ids"])]
            except (ValueError, TypeError, json.JSONDecodeError):
                material_ids = []
        if material_ids:
            placeholders = ",".join("?" for _ in material_ids)
            access_clause = "" if user["role"] == "admin" else " AND (owner_id=? OR source LIKE '내장 자료 · %')"
            parameters = tuple(material_ids) + (() if user["role"] == "admin" else (user["id"],))
            materials = connection.execute(
                f"SELECT id,category,source,length(content) AS characters,substr(content,1,360) AS preview FROM datasets WHERE status='approved' AND consent=1 AND id IN ({placeholders}){access_clause} ORDER BY CASE WHEN source LIKE '내장 자료 · %' THEN 0 ELSE 1 END,id DESC LIMIT 8",
                parameters,
            ).fetchall()
        elif user["role"] == "admin":
            materials = connection.execute(
                "SELECT id,category,source,length(content) AS characters,substr(content,1,360) AS preview FROM datasets WHERE status='approved' AND consent=1 ORDER BY CASE WHEN source LIKE '내장 자료 · %' THEN 0 ELSE 1 END,id DESC LIMIT 8"
            ).fetchall()
        else:
            materials = connection.execute(
                "SELECT id,category,source,length(content) AS characters,substr(content,1,360) AS preview FROM datasets WHERE status='approved' AND consent=1 AND (owner_id=? OR source LIKE '내장 자료 · %') ORDER BY CASE WHEN source LIKE '내장 자료 · %' THEN 0 ELSE 1 END,id DESC LIMIT 8",
                (user["id"],),
            ).fetchall()
    system = {"cpu_percent": None, "memory_percent": None, "memory_used_gb": None, "memory_total_gb": None,
              "gpu_name": torch.cuda.get_device_name(0) if torch.cuda.is_available() else None,
              "gpu_memory_used_gb": None}
    try:
        import psutil
        system.update({"cpu_percent": psutil.cpu_percent(interval=None),
                       "memory_percent": psutil.virtual_memory().percent,
                       "memory_used_gb": round(psutil.virtual_memory().used / 1024**3, 2),
                       "memory_total_gb": round(psutil.virtual_memory().total / 1024**3, 2)})
    except ImportError:
        pass
    if torch.cuda.is_available():
        system["gpu_memory_used_gb"] = round(torch.cuda.memory_allocated(0) / 1024**3, 2)
    return {"model": active_metadata(), "approved_data_count": approved, "pending_data_count": pending,
            "completed_training_count": jobs, "recent_jobs": [dict(row) for row in recent_jobs],
            "versions": [dict(row) for row in versions], "metrics": list(reversed([dict(row) for row in metrics])),
            "learning_materials": [dict(row) for row in materials],
            "learning_materials_status": material_job["status"] if material_job else "ready",
            "system": system}


@app.get("/api/training/jobs")
def list_jobs(user=Depends(admin_user)):
    with database.connect() as connection:
        rows = connection.execute("SELECT * FROM training_jobs ORDER BY created_at DESC LIMIT 50").fetchall()
    return [{key: row[key] for key in row.keys() if key not in {"dataset_ids", "checkpoint_path", "candidate_path"}} for row in rows]


@app.get("/api/training/auto")
def get_auto_training(user=Depends(admin_user)):
    with database.connect() as connection:
        return {"enabled": setting_value(connection, "auto_training_enabled", "1") == "1",
                "min_new_items": int(setting_value(connection, "auto_training_min_items", "1"))}


@app.put("/api/training/auto")
def set_auto_training(payload: AutoTrainingInput, user=Depends(admin_user)):
    with database.transaction() as connection:
        connection.execute("INSERT INTO settings(key,value,updated_at) VALUES('auto_training_enabled',?,CURRENT_TIMESTAMP) ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=CURRENT_TIMESTAMP", ("1" if payload.enabled else "0",))
        connection.execute("INSERT INTO settings(key,value,updated_at) VALUES('auto_training_min_items',?,CURRENT_TIMESTAMP) ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=CURRENT_TIMESTAMP", (str(payload.min_new_items),))
    return {"enabled": payload.enabled, "min_new_items": payload.min_new_items}


@app.get("/api/training/jobs/{job_id}")
def get_job(job_id: str, user=Depends(admin_user)):
    with database.connect() as connection:
        row = connection.execute("SELECT * FROM training_jobs WHERE id=?", (job_id,)).fetchone()
        metrics = connection.execute("SELECT epoch,training_loss,validation_loss,validation_accuracy,created_at FROM training_metrics WHERE job_id=? ORDER BY epoch", (job_id,)).fetchall()
    if not row:
        raise HTTPException(404, "학습 작업을 찾을 수 없습니다.")
    job = {key: row[key] for key in row.keys() if key not in {"dataset_ids", "checkpoint_path", "candidate_path"}}
    return {"job": job, "metrics": [dict(metric) for metric in metrics]}


@app.post("/api/training/start")
def start_training(payload: TrainingInput, user=Depends(admin_user)):
    with database.transaction() as connection:
        active_job = connection.execute("SELECT id,status FROM training_jobs WHERE status IN ('queued','running','paused') LIMIT 1").fetchone()
        if active_job:
            raise HTTPException(409, f"진행 중이거나 일시 중단된 학습 작업이 있습니다: {active_job['id']}")
        rows = connection.execute("SELECT id,length(content) AS characters FROM datasets WHERE consent=1 AND status='approved' ORDER BY id").fetchall()
        dataset_ids = [row["id"] for row in rows]
        if not dataset_ids or sum(row["characters"] for row in rows) < 20:
            raise HTTPException(422, "먼저 동의한 학습 데이터를 승인해 주세요.")
        job_id = uuid.uuid4().hex
        connection.execute("INSERT INTO training_jobs(id,owner_id,status,total_epochs,dataset_ids,example_count) VALUES(?,?, 'queued',?,?,?)",
                            (job_id,user["id"],payload.epochs,json.dumps(dataset_ids),len(dataset_ids)))
    try:
        pid = launch_worker(job_id)
    except Exception as error:
        with database.transaction() as connection:
            connection.execute("UPDATE training_jobs SET status='failed',error=? WHERE id=?", (str(error)[:1000],job_id))
        raise HTTPException(500, "학습 프로세스를 시작하지 못했습니다.")
    return {"id": job_id, "status": "queued", "pid": pid}


@app.post("/api/training/jobs/{job_id}/pause")
def pause_job(job_id: str, user=Depends(admin_user)):
    with database.transaction() as connection:
        cursor = connection.execute("UPDATE training_jobs SET pause_requested=1 WHERE id=? AND status='running'", (job_id,))
        if not cursor.rowcount:
            raise HTTPException(409, "실행 중인 학습 작업이 아닙니다.")
    return {"requested": True}


@app.post("/api/training/jobs/{job_id}/resume")
def resume_job(job_id: str, user=Depends(admin_user)):
    with database.transaction() as connection:
        row = connection.execute("SELECT status FROM training_jobs WHERE id=?", (job_id,)).fetchone()
        if not row or row["status"] != "paused":
            raise HTTPException(409, "재개할 수 있는 일시 중단 작업이 아닙니다.")
        connection.execute("UPDATE training_jobs SET status='queued',pause_requested=0,cancel_requested=0,error=NULL,finished_at=NULL WHERE id=?", (job_id,))
    try:
        pid = launch_worker(job_id, resume=True)
    except Exception as error:
        with database.transaction() as connection:
            connection.execute("UPDATE training_jobs SET status='paused',error=? WHERE id=?", (str(error)[:1000],job_id))
        raise HTTPException(500, "학습 재개 프로세스를 시작하지 못했습니다.")
    return {"requested": True, "pid": pid}


@app.post("/api/training/jobs/{job_id}/cancel")
def cancel_job(job_id: str, user=Depends(admin_user)):
    with database.transaction() as connection:
        row = connection.execute("SELECT status FROM training_jobs WHERE id=?", (job_id,)).fetchone()
        if not row or row["status"] not in {"queued", "running", "paused"}:
            raise HTTPException(409, "중단할 수 있는 학습 작업이 아닙니다.")
        if row["status"] in {"paused", "queued"}:
            connection.execute("UPDATE training_jobs SET status='cancelled',cancel_requested=1,finished_at=CURRENT_TIMESTAMP WHERE id=?", (job_id,))
        else:
            connection.execute("UPDATE training_jobs SET cancel_requested=1 WHERE id=?", (job_id,))
    return {"requested": True}


@app.get("/api/models/versions")
def list_versions(user=Depends(current_user)):
    with database.connect() as connection:
        rows = connection.execute("SELECT version,validation_loss,active,created_at FROM model_versions ORDER BY created_at DESC").fetchall()
    return [dict(row) for row in rows]


@app.post("/api/models/rollback/{version}")
def rollback(version: str, user=Depends(admin_user)):
    with database.transaction() as connection:
        busy = connection.execute("SELECT 1 FROM training_jobs WHERE status IN ('queued','running','paused') LIMIT 1").fetchone()
        row = connection.execute("SELECT * FROM model_versions WHERE version=?", (version,)).fetchone()
        if busy:
            raise HTTPException(409, "학습 중에는 모델을 교체할 수 없습니다.")
        if not row or not Path(row["checkpoint_path"]).exists():
            raise HTTPException(404, "복구할 모델 버전을 찾을 수 없습니다.")
        checkpoint = torch.load(row["checkpoint_path"], map_location="cpu", weights_only=False)
        checkpoint["version"] = version
        atomic_save(checkpoint, ACTIVE_PATH)
        connection.execute("UPDATE model_versions SET active=0")
        connection.execute("UPDATE model_versions SET active=1 WHERE version=?", (version,))
    return {"active_version": version}


@app.delete("/api/account/data")
def delete_personal_data(user=Depends(current_user)):
    with database.transaction() as connection:
        dataset_ids = {row["id"] for row in connection.execute("SELECT id FROM datasets WHERE owner_id=?", (user["id"],)).fetchall()}
        ensure_not_training(connection, dataset_ids)
        connection.execute("DELETE FROM conversations WHERE user_id=?", (user["id"],))
        connection.execute("DELETE FROM memories WHERE user_id=?", (user["id"],))
        connection.execute("DELETE FROM datasets WHERE owner_id=?", (user["id"],))
    return {"deleted": True}

