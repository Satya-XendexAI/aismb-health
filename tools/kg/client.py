import os
import ssl

from dotenv import load_dotenv
from neo4j import GraphDatabase
from openai import OpenAI

load_dotenv()

# ── Neo4j ──────────────────────────────────────────────────────────────────────

_URI      = os.getenv("NEO4J_URI",      "bolt://localhost:7687")
_USER     = os.getenv("NEO4J_USERNAME", "neo4j")
_PASSWORD = os.getenv("NEO4J_PASSWORD", "")
_DB       = os.getenv("NEO4J_DATABASE", "neo4j")
_INSECURE = os.getenv("NEO4J_INSECURE", "false").lower() in ("1", "true", "yes", "on")

TENANT_ID     = os.getenv("TENANT_ID",     "glh-chn")
HOSPITAL_NAME = os.getenv("HOSPITAL_NAME", "Hospital")


def _make_driver():
    uri = _URI
    if _INSECURE:
        if "+s://" in uri:
            uri = uri.replace("+s://", "+ssc://", 1)
            return GraphDatabase.driver(uri, auth=(_USER, _PASSWORD))
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return GraphDatabase.driver(uri, auth=(_USER, _PASSWORD), ssl_context=ctx)
    return GraphDatabase.driver(uri, auth=(_USER, _PASSWORD))


driver = _make_driver()
database = _DB

# ── Gemini clients (OpenAI-compatible) ────────────────────────────────────────
 
gemini_client = OpenAI(
    api_key=os.getenv("GEMINI_API_KEY", ""),
    base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
    timeout=60.0,
)
 
embed_client = OpenAI(
    api_key=os.getenv("GEMINI_API_KEY", ""),
    base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
    timeout=30.0,
)
 
# Warmup embedding client on import (avoids cold-start latency on first request)
try:
    embed_client.embeddings.create(input="warmup", model=os.getenv("GEMINI_EMBED_MODEL", "models/gemini-embedding-001"))
except Exception:
    pass
EMBED_MODEL   = os.getenv("GEMINI_EMBED_MODEL", "models/gemini-embedding-001")
PARSE_MODEL   = os.getenv("GEMINI_PARSE_MODEL", "models/gemini-3.5-flash-lite")
