import base64
import hashlib
import io
import json
import os
import re
import secrets
import sqlite3
import uuid
from datetime import date
from pathlib import Path
from typing import Literal

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel, field_validator
from PIL import Image, ImageDraw, ImageFont
from openai import OpenAI

ROOT = Path(__file__).resolve().parent.parent
FRONTEND = ROOT / "frontend"
OUTPUTS = ROOT / "outputs"
OUTPUTS.mkdir(exist_ok=True)
ANDROID_APK = ROOT / "android" / "app" / "build" / "outputs" / "apk" / "debug" / "app-debug.apk"

load_dotenv(ROOT / ".env")

DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
DATABASE = Path(os.getenv("DADA_DATABASE", ROOT / "dada.db"))
SESSION_DAYS = 30
FREE_GENERATIONS_PER_DAY = int(os.getenv("DADA_FREE_GENERATIONS_PER_DAY", "10"))
COOKIE_SAMESITE = os.getenv("DADA_COOKIE_SAMESITE", "lax")

app = FastAPI(title="DADA IA", version="1.0.0")
allowed_origins = [
    origin.strip()
    for origin in os.getenv(
        "DADA_ALLOWED_ORIGINS",
        "https://app.dada-ia.example,capacitor://localhost,http://localhost",
    ).split(",")
    if origin.strip()
]
if os.getenv("DADA_COOKIE_SECURE", "0") != "1" and "null" not in allowed_origins:
    allowed_origins.append("null")
app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_origin_regex=r"^https?://(localhost|127\.0\.0\.1)(:\d+)?$"
    if os.getenv("DADA_COOKIE_SECURE", "0") != "1"
    else None,
    allow_credentials=True,
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type"],
)


class DatabaseConnection:
    def __init__(self):
        self.is_postgres = bool(DATABASE_URL)
        if self.is_postgres:
            import psycopg
            from psycopg.rows import dict_row

            self.connection = psycopg.connect(DATABASE_URL, row_factory=dict_row)
        else:
            self.connection = sqlite3.connect(DATABASE)
            self.connection.row_factory = sqlite3.Row

    def __enter__(self):
        return self

    def __exit__(self, exception_type, exception, traceback):
        if exception:
            self.connection.rollback()
        self.connection.close()

    def execute(self, query, parameters=()):
        if self.is_postgres:
            query = query.replace("?", "%s")
            return self.connection.execute(query, parameters)
        return self.connection.execute(query, parameters)

    def commit(self):
        self.connection.commit()

    def rollback(self):
        self.connection.rollback()


def database_connection():
    return DatabaseConnection()


def initialize_database():
    with database_connection() as connection:
        identity = "SERIAL PRIMARY KEY" if connection.is_postgres else "INTEGER PRIMARY KEY AUTOINCREMENT"
        timestamp = "TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP"
        connection.execute(
            f"""
            CREATE TABLE IF NOT EXISTS users (
                id {identity},
                name TEXT NOT NULL,
                email TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                created_at {timestamp}
            )
            """
        )
        connection.execute(
            f"""
            CREATE TABLE IF NOT EXISTS sessions (
                token TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                created_at {timestamp},
                FOREIGN KEY (user_id) REFERENCES users (id)
            )
            """
        )
        connection.execute(
            f"""
            CREATE TABLE IF NOT EXISTS projects (
                id {identity},
                user_id INTEGER NOT NULL,
                job_id TEXT NOT NULL UNIQUE,
                prompt TEXT NOT NULL,
                title TEXT NOT NULL DEFAULT '',
                subtitle TEXT NOT NULL DEFAULT '',
                width INTEGER NOT NULL,
                height INTEGER NOT NULL,
                output_format TEXT NOT NULL DEFAULT 'png',
                created_at {timestamp},
                FOREIGN KEY (user_id) REFERENCES users (id)
            )
            """
        )
        connection.execute(
            f"""
            CREATE TABLE IF NOT EXISTS generation_usage (
                user_id INTEGER NOT NULL,
                usage_date TEXT NOT NULL,
                generation_count INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (user_id, usage_date),
                FOREIGN KEY (user_id) REFERENCES users (id)
            )
            """
        )
        columns = (
            connection.execute(
                "SELECT column_name AS name FROM information_schema.columns WHERE table_name = 'projects'"
            ).fetchall()
            if connection.is_postgres
            else connection.execute("PRAGMA table_info(projects)").fetchall()
        )
        if not any(column["name"] == "output_format" for column in columns):
            connection.execute(
                "ALTER TABLE projects ADD COLUMN output_format TEXT NOT NULL DEFAULT 'png'"
            )
        connection.commit()


initialize_database()


def normalize_email(value: str) -> str:
    return value.strip().lower()


def hash_password(password: str, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 120_000)
    return f"{salt.hex()}${digest.hex()}"


def verify_password(password: str, stored_hash: str) -> bool:
    try:
        salt_hex, digest_hex = stored_hash.split("$", 1)
        expected = hashlib.pbkdf2_hmac(
            "sha256", password.encode(), bytes.fromhex(salt_hex), 120_000
        ).hex()
        return secrets.compare_digest(expected, digest_hex)
    except (ValueError, TypeError):
        return False


def current_user(request: Request):
    session_token = request.cookies.get("dada_session")
    if not session_token:
        return None
    with database_connection() as connection:
        if connection.is_postgres:
            query = """
                SELECT users.id, users.name, users.email
                FROM sessions JOIN users ON users.id = sessions.user_id
                WHERE sessions.token = ?
                  AND sessions.created_at >= CURRENT_TIMESTAMP - (? * INTERVAL '1 day')
            """
            parameters = (session_token, SESSION_DAYS)
        else:
            query = """
                SELECT users.id, users.name, users.email
                FROM sessions JOIN users ON users.id = sessions.user_id
                WHERE sessions.token = ?
                  AND sessions.created_at >= datetime('now', ?)
            """
            parameters = (session_token, f"-{SESSION_DAYS} days")
        return connection.execute(query, parameters).fetchone()


def create_session(user_id: int) -> str:
    token = secrets.token_urlsafe(32)
    with database_connection() as connection:
        connection.execute(
            "INSERT INTO sessions (token, user_id) VALUES (?, ?)",
            (token, user_id),
        )
        connection.commit()
    return token


def session_cookie(response: Response, user_id: int):
    response.set_cookie(
        "dada_session",
        create_session(user_id),
        httponly=True,
        secure=os.getenv("DADA_COOKIE_SECURE", "0") == "1",
        samesite=COOKIE_SAMESITE,
        max_age=60 * 60 * 24 * SESSION_DAYS,
    )


def reserve_generation(user_id: int):
    usage_date = date.today().isoformat()
    with database_connection() as connection:
        row = connection.execute(
            "SELECT generation_count FROM generation_usage WHERE user_id = ? AND usage_date = ?",
            (user_id, usage_date),
        ).fetchone()
        count = row["generation_count"] if row else 0
        if count >= FREE_GENERATIONS_PER_DAY:
            raise HTTPException(
                429,
                f"Quota quotidienne atteinte ({FREE_GENERATIONS_PER_DAY} générations).",
            )
        if row:
            connection.execute(
                "UPDATE generation_usage SET generation_count = generation_count + 1 WHERE user_id = ? AND usage_date = ?",
                (user_id, usage_date),
            )
        else:
            connection.execute(
                "INSERT INTO generation_usage (user_id, usage_date, generation_count) VALUES (?, ?, 1)",
                (user_id, usage_date),
            )
        connection.commit()


def release_generation(user_id: int):
    usage_date = date.today().isoformat()
    with database_connection() as connection:
        connection.execute(
            """
            UPDATE generation_usage
            SET generation_count = CASE
                WHEN generation_count > 0 THEN generation_count - 1
                ELSE 0
            END
            WHERE user_id = ? AND usage_date = ?
            """,
            (user_id, usage_date),
        )
        connection.commit()


def generation_usage(user_id: int):
    usage_date = date.today().isoformat()
    with database_connection() as connection:
        row = connection.execute(
            "SELECT generation_count FROM generation_usage WHERE user_id = ? AND usage_date = ?",
            (user_id, usage_date),
        ).fetchone()
    used = row["generation_count"] if row else 0
    return {"used": used, "limit": FREE_GENERATIONS_PER_DAY, "remaining": max(0, FREE_GENERATIONS_PER_DAY - used)}


def sanitize_text(value: str | None, max_length: int = 120) -> str:
    text = (value or "").strip()
    text = re.sub(r"\s+", " ", text)
    return text[:max_length]


class GenerateRequest(BaseModel):
    prompt: str
    width: int = 1080
    height: int = 1350
    title: str = ""
    subtitle: str = ""
    output_format: Literal["png", "jpeg", "psd"] = "png"

    @field_validator("prompt")
    @classmethod
    def validate_prompt(cls, value: str) -> str:
        cleaned = sanitize_text(value, max_length=2000)
        if not cleaned:
            raise ValueError("Le prompt est obligatoire.")
        return cleaned

    @field_validator("width", "height")
    @classmethod
    def validate_size(cls, value: int, info) -> int:
        if value < 512 or value > 4096:
            raise ValueError(f"{info.field_name} doit être compris entre 512 et 4096 pixels.")
        return value

    @field_validator("title", "subtitle")
    @classmethod
    def validate_text_layer(cls, value: str, info) -> str:
        return sanitize_text(value, max_length=120)


class AccountRequest(BaseModel):
    name: str
    email: str
    password: str

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        cleaned = sanitize_text(value, max_length=80)
        if len(cleaned) < 2:
            raise ValueError("Le nom doit contenir au moins 2 caractères.")
        return cleaned

    @field_validator("email")
    @classmethod
    def validate_email(cls, value: str) -> str:
        cleaned = normalize_email(value)
        if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", cleaned):
            raise ValueError("Adresse email invalide.")
        return cleaned

    @field_validator("password")
    @classmethod
    def validate_password(cls, value: str) -> str:
        if len(value) < 8:
            raise ValueError("Le mot de passe doit contenir au moins 8 caractères.")
        return value


class LoginRequest(BaseModel):
    email: str
    password: str

    @field_validator("email")
    @classmethod
    def validate_email(cls, value: str) -> str:
        return normalize_email(value)


def font(size=64, bold=False):
    candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "C:/Windows/Fonts/arialbd.ttf" if bold else "C:/Windows/Fonts/arial.ttf",
    ]
    for p in candidates:
        if Path(p).exists():
            return ImageFont.truetype(p, size)
    return ImageFont.load_default()

def make_psd(base: Image.Image, title: str, subtitle: str, out: Path):
    # Build a stable composition contract: background, generated objects, then text.
    # The generated image is still one raster object layer until segmentation is added.
    from psd_tools import PSDImage
    from psd_tools.api.layers import PixelLayer

    psd = PSDImage.new(mode="RGB", size=base.size)
    background = Image.new("RGB", base.size, (8, 10, 14))
    bg = PixelLayer.frompil(background, psd, name="Background")
    psd.append(bg)

    objects = PixelLayer.frompil(base.convert("RGB"), psd, name="Objets")
    psd.append(objects)

    if title:
        overlay = Image.new("RGBA", base.size, (0,0,0,0))
        d = ImageDraw.Draw(overlay)
        f = font(max(32, base.width // 16), True)
        box = d.textbbox((0,0), title, font=f)
        x = (base.width - (box[2]-box[0])) // 2
        y = int(base.height * .08)
        d.text((x,y), title, font=f, fill="white", stroke_width=2, stroke_fill="black")
        psd.append(PixelLayer.frompil(overlay, psd, name="Text Layer - Titre"))

    if subtitle:
        overlay = Image.new("RGBA", base.size, (0,0,0,0))
        d = ImageDraw.Draw(overlay)
        f = font(max(24, base.width // 30), False)
        box = d.textbbox((0,0), subtitle, font=f)
        x = (base.width - (box[2]-box[0])) // 2
        y = int(base.height * .18)
        d.text((x,y), subtitle, font=f, fill="white", stroke_width=1, stroke_fill="black")
        psd.append(PixelLayer.frompil(overlay, psd, name="Text Layer - Sous-titre"))

    psd.save(out)


def build_composition_analysis(prompt: str, title: str, subtitle: str, width: int, height: int):
    return {
        "version": 1,
        "status": "composition-ready",
        "source": "generated-raster",
        "canvas": {"width": width, "height": height},
        "layers": [
            {"name": "Background", "type": "background", "extracted": False},
            {"name": "Objets", "type": "objects", "extracted": False},
            {
                "name": "Text Layer - Titre",
                "type": "text",
                "value": title,
                "extracted": bool(title),
            },
            {
                "name": "Text Layer - Sous-titre",
                "type": "text",
                "value": subtitle,
                "extracted": bool(subtitle),
            },
        ],
        "prompt": prompt,
        "next_step": "segmentation-and-ocr",
    }

def extract_image(result):
    data = getattr(result, "data", None) or []
    if not data:
        raise RuntimeError("Le modèle d'image n'a retourné aucune image.")
    item = data[0]
    b64 = getattr(item, "b64_json", None)
    if b64:
        return base64.b64decode(b64)
    url = getattr(item, "url", None)
    if url:
        from urllib.parse import urlparse

        if urlparse(url).scheme != "https":
            raise RuntimeError("URL image non sécurisée.")
        import urllib.request

        with urllib.request.urlopen(url, timeout=30) as response:  # nosec B310 - scheme restricted to HTTPS above
            return response.read()
    raise RuntimeError("Réponse image inconnue.")

@app.get("/", response_class=HTMLResponse)
def home():
    return (FRONTEND / "index.html").read_text(encoding="utf-8")


@app.get("/manifest.webmanifest")
def manifest():
    return FileResponse(FRONTEND / "manifest.webmanifest", media_type="application/manifest+json")


@app.get("/mobile-config.js")
def mobile_config():
    return FileResponse(FRONTEND / "mobile-config.js", media_type="application/javascript")


@app.get("/icons/{name}")
def icon(name: str):
    safe_name = Path(name).name
    if safe_name not in {"dada-192.svg", "dada-512.svg"}:
        raise HTTPException(404, "Icône introuvable.")
    return FileResponse(FRONTEND / "icons" / safe_name, media_type="image/svg+xml")


@app.get("/download/android")
def android_download():
    if not ANDROID_APK.exists():
        raise HTTPException(404, "APK Android indisponible. Compilez la version Android avant le téléchargement.")
    return FileResponse(
        ANDROID_APK,
        media_type="application/vnd.android.package-archive",
        filename="dada-ia.apk",
    )


@app.get("/sw.js")
def service_worker():
    return FileResponse(FRONTEND / "sw.js", media_type="application/javascript")


@app.get("/health")
def health():
    return {"status": "ok", "service": "DADA IA"}


@app.get("/api/me")
def me(request: Request):
    user = current_user(request)
    if not user:
        raise HTTPException(401, "Un compte est nécessaire pour continuer.")
    return {"id": user["id"], "name": user["name"], "email": user["email"]}


@app.get("/api/usage")
def usage(request: Request):
    user = current_user(request)
    if not user:
        raise HTTPException(401, "Créez un compte pour continuer.")
    return generation_usage(user["id"])


@app.post("/api/account")
def create_account(account: AccountRequest, response: Response):
    try:
        with database_connection() as connection:
            if connection.is_postgres:
                cursor = connection.execute(
                    "INSERT INTO users (name, email, password_hash) VALUES (?, ?, ?) RETURNING id",
                    (account.name, account.email, hash_password(account.password)),
                )
                user_id = cursor.fetchone()["id"]
            else:
                cursor = connection.execute(
                    "INSERT INTO users (name, email, password_hash) VALUES (?, ?, ?)",
                    (account.name, account.email, hash_password(account.password)),
                )
                user_id = cursor.lastrowid
            connection.commit()
    except Exception as error:
        if isinstance(error, sqlite3.IntegrityError) or getattr(error, "sqlstate", None) == "23505":
            raise HTTPException(409, "Un compte existe déjà avec cet email.") from error
        raise

    session_cookie(response, user_id)
    return {"message": "Compte créé.", "name": account.name}


@app.post("/api/login")
def login(credentials: LoginRequest, response: Response):
    with database_connection() as connection:
        user = connection.execute(
            "SELECT id, name, email, password_hash FROM users WHERE email = ?",
            (credentials.email,),
        ).fetchone()

    if not user or not verify_password(credentials.password, user["password_hash"]):
        raise HTTPException(401, "Email ou mot de passe incorrect.")

    session_cookie(response, user["id"])
    return {"message": "Connexion réussie.", "name": user["name"]}


@app.post("/api/logout")
def logout(request: Request, response: Response):
    token = request.cookies.get("dada_session")
    if token:
        with database_connection() as connection:
            connection.execute("DELETE FROM sessions WHERE token = ?", (token,))
            connection.commit()
    response.delete_cookie("dada_session")
    return {"message": "Déconnexion réussie."}


@app.get("/api/projects")
def projects(request: Request):
    user = current_user(request)
    if not user:
        raise HTTPException(401, "Créez un compte pour continuer.")
    with database_connection() as connection:
        rows = connection.execute(
            """
            SELECT job_id, prompt, title, subtitle, width, height, output_format, created_at
            FROM projects
            WHERE user_id = ?
            ORDER BY created_at DESC, id DESC
            LIMIT 30
            """,
            (user["id"],),
        ).fetchall()
    return [
        {
            "id": row["job_id"],
            "prompt": row["prompt"],
            "title": row["title"],
            "subtitle": row["subtitle"],
            "width": row["width"],
            "height": row["height"],
            "format": row["output_format"],
            "created_at": row["created_at"],
            "preview": f"/outputs/{row['job_id']}.png",
            "psd": f"/outputs/{row['job_id']}.psd",
            "download": (
                f"/outputs/{row['job_id']}.psd"
                if row["output_format"] == "psd"
                else f"/outputs/{row['job_id']}.jpg"
                if row["output_format"] == "jpeg"
                else f"/outputs/{row['job_id']}.png"
            ),
            "analysis": f"/outputs/{row['job_id']}.analysis.json",
        }
        for row in rows
    ]


@app.post("/api/generate")
def generate(req: GenerateRequest, request: Request):
    user = current_user(request)
    if not user:
        raise HTTPException(401, "Créez un compte pour continuer.")
    key = os.getenv("OPENAI_API_KEY")
    if not key:
        raise HTTPException(500, "OPENAI_API_KEY manquante dans .env")
    reserve_generation(user["id"])

    req.prompt = sanitize_text(req.prompt, max_length=2000)
    req.title = sanitize_text(req.title, max_length=120)
    req.subtitle = sanitize_text(req.subtitle, max_length=120)

    job = uuid.uuid4().hex
    png = OUTPUTS / f"{job}.png"
    jpeg = OUTPUTS / f"{job}.jpg"
    psd = OUTPUTS / f"{job}.psd"
    analysis = OUTPUTS / f"{job}.analysis.json"

    try:
        client = OpenAI(api_key=key)
        result = client.images.generate(
            model=os.getenv("OPENAI_IMAGE_MODEL", "gpt-image-1"),
            prompt=req.prompt,
            size="1024x1536" if req.height > req.width else "1536x1024",
        )
        raw = extract_image(result)
        image = Image.open(io.BytesIO(raw)).convert("RGB")
        image = image.resize((req.width, req.height), Image.Resampling.LANCZOS)
        image.save(png, "PNG")
        if req.output_format == "jpeg":
            image.save(jpeg, "JPEG", quality=95, optimize=True)
        elif req.output_format == "psd":
            make_psd(image, req.title.strip(), req.subtitle.strip(), psd)
        analysis.write_text(
            json.dumps(
                build_composition_analysis(
                    req.prompt, req.title, req.subtitle, req.width, req.height
                ),
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        with database_connection() as connection:
            connection.execute(
                """
                INSERT INTO projects
                    (user_id, job_id, prompt, title, subtitle, width, height, output_format)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    user["id"],
                    job,
                    req.prompt,
                    req.title,
                    req.subtitle,
                    req.width,
                    req.height,
                    req.output_format,
                ),
            )
            connection.commit()
    except Exception as e:
        release_generation(user["id"])
        for output_path in (png, jpeg, psd, analysis):
            output_path.unlink(missing_ok=True)
        raise HTTPException(500, f"Génération impossible : {e}")

    return {
        "id": job,
        "preview": f"/outputs/{job}.png",
        "psd": f"/outputs/{job}.psd",
        "download": (
            f"/outputs/{job}.psd"
            if req.output_format == "psd"
            else f"/outputs/{job}.jpg"
            if req.output_format == "jpeg"
            else f"/outputs/{job}.png"
        ),
        "format": req.output_format,
        "analysis": f"/outputs/{job}.analysis.json",
        "message": f"Visuel {req.output_format.upper()} créé."
    }

@app.get("/outputs/{name}")
def output(name: str, request: Request):
    user = current_user(request)
    if not user:
        raise HTTPException(401, "Créez un compte pour accéder à ce fichier.")
    safe = Path(name).name
    job_id = safe.removesuffix(".analysis.json").rsplit(".", 1)[0]
    with database_connection() as connection:
        project = connection.execute(
            "SELECT 1 FROM projects WHERE job_id = ? AND user_id = ?",
            (job_id, user["id"]),
        ).fetchone()
    if not project:
        raise HTTPException(404, "Fichier introuvable.")
    path = OUTPUTS / safe
    if not path.exists():
        raise HTTPException(404, "Fichier introuvable.")
    return FileResponse(path)
