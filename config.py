"""Local secrets and explicit Telegram access scopes."""
import os
from dataclasses import dataclass, field
from pathlib import Path
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
os.environ["HAYSTACK_TELEMETRY_ENABLED"] = "false"
os.environ["HAYSTACK_CONTENT_TRACING_ENABLED"] = "false"
load_dotenv(ROOT / ".env")

@dataclass(frozen=True)
class Config:
    api_key: str = field(repr=False)
    pinecone_key: str = field(repr=False)
    index: str
    base_url: str = ""
    model: str = "gpt-6-luna"
    embedding_model: str = "text-embedding-3-small"
    token: str = field(default="", repr=False)
    admins: tuple[int, ...] = ()
    chats: tuple[int, ...] = ()
    prefix: str = "vpg08-team"
    database: Path = ROOT / "data" / "team.sqlite3"

def ids(name):
    return tuple(int(v.strip()) for v in os.getenv(name, "").split(",") if v.strip())

def load_config(require_bot=False):
    names = ["OPENAI_API_KEY", "PINECONE_API_KEY", "PINECONE_INDEX_NAME"]
    if require_bot:
        names += ["TELEGRAM_BOT_TOKEN", "ALLOWED_USER_IDS"]
    missing = [n for n in names if not os.getenv(n, "").strip()]
    if missing:
        raise ValueError("Заполните .env: " + ", ".join(missing))
    return Config(api_key=os.environ["OPENAI_API_KEY"], pinecone_key=os.environ["PINECONE_API_KEY"],
        index=os.environ["PINECONE_INDEX_NAME"], base_url=os.getenv("OPENAI_BASE_URL", ""),
        model=os.getenv("CHAT_MODEL", "gpt-6-luna"),
        embedding_model=os.getenv("EMBEDDING_MODEL", "text-embedding-3-small"),
        token=os.getenv("TELEGRAM_BOT_TOKEN", ""), admins=ids("ALLOWED_USER_IDS"),
        chats=ids("ALLOWED_CHAT_IDS"))
