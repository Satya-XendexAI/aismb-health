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
# NOTE: no module-level TENANT_ID/HOSPITAL_NAME here anymore — the Neo4j
# tenant to search is resolved per-request from the session's hospital_id
# (see config/kg_tenants.py + tools/kg/context.py), not fixed at process
# startup. A global constant meant every search always hit the same tenant
# regardless of which hospital the patient was actually messaging.

gemini_client = OpenAI(
    api_key=os.getenv("GEMINI_API_KEY", ""),
    base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
)

embed_client  = gemini_client
EMBED_MODEL   = os.getenv("GEMINI_EMBED_MODEL", "models/gemini-embedding-001")
PARSE_MODEL   = os.getenv("GEMINI_PARSE_MODEL", "models/gemini-3.5-flash-lite")
