import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = Path(os.getenv("DATA_DIR", ROOT / "data")).resolve()
DB_PATH = Path(os.getenv("DATABASE_PATH", DATA_DIR / "app.sqlite3")).resolve()
MODEL_DIR = Path(os.getenv("MODEL_DIR", DATA_DIR / "models")).resolve()
FRONTEND_DIR = ROOT / "frontend"
SECRET_KEY = os.getenv("SECRET_KEY", "development-only-change-before-deploy")
BOOTSTRAP_ADMIN_EMAIL = os.getenv("BOOTSTRAP_ADMIN_EMAIL", "").strip().lower()
GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID", "").strip()
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET", "").strip()
GOOGLE_REDIRECT_URI = os.getenv("GOOGLE_REDIRECT_URI", "").strip()
MAX_UPLOAD_BYTES = 5 * 1024 * 1024
MAX_TEXT_CHARS = 200_000
DEFAULT_MODEL_CONFIG = {
    "vocab_size": 258,
    "block_size": int(os.getenv("MODEL_BLOCK_SIZE", "128")),
    "n_embd": int(os.getenv("MODEL_EMBED_DIM", "96")),
    "n_head": int(os.getenv("MODEL_HEADS", "4")),
    "n_layer": int(os.getenv("MODEL_LAYERS", "2")),
    "dropout": 0.1,
}
MAX_RESPONSE_TOKENS = int(os.getenv("MAX_RESPONSE_TOKENS", "96"))

DATA_DIR.mkdir(parents=True, exist_ok=True)
MODEL_DIR.mkdir(parents=True, exist_ok=True)
