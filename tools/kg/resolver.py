import json
import difflib

from prompts.kg_retriever import KG_PARSE_SYSTEM
from tools.kg.client import driver, database, gemini_client, PARSE_MODEL
from orchestrator.tracing import traced, record_usage

_SPEC_NAMES_CACHE = None


def _specialization_names() -> list[str]:
    global _SPEC_NAMES_CACHE
    if _SPEC_NAMES_CACHE is None:
        try:
            with driver.session(database=database) as s:
                rows = s.execute_read(
                    lambda tx: tx.run("MATCH (sp:Specialization) RETURN sp.name AS name").data()
                )
            _SPEC_NAMES_CACHE = [r["name"] for r in rows]
        except Exception:
            _SPEC_NAMES_CACHE = []
    return _SPEC_NAMES_CACHE


def resolve_specializations(specs) -> list[str]:
    """Map LLM-provided specialization strings onto real graph node names via
    substring match then fuzzy fallback. Fully dynamic — nothing hardcoded."""
    if isinstance(specs, str):
        specs = [specs]
    names       = _specialization_names()
    lower_names = [n.lower() for n in names]
    resolved: list[str] = []
    for sp in specs or []:
        sp_l = (sp or "").lower().strip()
        if not sp_l:
            continue
        exact = [n for n, nl in zip(names, lower_names) if sp_l in nl or nl in sp_l]
        if exact:
            resolved.extend(exact)
            continue
        close = difflib.get_close_matches(sp_l, lower_names, n=1, cutoff=0.75)
        if close:
            resolved.append(names[lower_names.index(close[0])])
        else:
            resolved.append(sp)
    seen, out = set(), []
    for r in resolved:
        if r not in seen:
            seen.add(r)
            out.append(r)
    return out


@traced("kg_retriever._parse_query", run_type="llm", tags=["kg", "llm"])
def parse_query(query: str) -> dict:
    resp = gemini_client.chat.completions.create(
        model=PARSE_MODEL,
        messages=[
            {"role": "system", "content": KG_PARSE_SYSTEM},
            {"role": "user",   "content": query},
        ],
        response_format={"type": "json_object"},
        temperature=0.0,
        max_tokens=200,
    )
    record_usage(resp, model=PARSE_MODEL)
    try:
        parsed = json.loads(resp.choices[0].message.content)
    except json.JSONDecodeError:
        parsed = {}
    parsed.setdefault("specializations", [])
    parsed.setdefault("doctor_name",     None)
    parsed.setdefault("language",        None)
    parsed.setdefault("min_experience",  None)
    return parsed
