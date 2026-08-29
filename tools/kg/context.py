from tools.kg.client import HOSPITAL_NAME, TENANT_ID
from tools.kg.resolver import parse_query, resolve_specializations
from tools.kg.queries import (
    find_doctors_by_specialization,
    find_doctors_by_language,
    find_by_fulltext,
    semantic_search,
    next_available_slots,
)


def _fuse_results(vector_results: list[dict], graph_results: list[dict], n: int = 6) -> list[dict]:
    scores:   dict[str, float] = {}
    doc_data: dict[str, dict]  = {}
    k = 60
    for rank, r in enumerate(vector_results):
        did = r["metadata"].get("doctor_id") or r["metadata"].get("sql_id") or r["id"]
        scores[did] = scores.get(did, 0) + 1 / (k + rank + 1)
        if did not in doc_data:
            doc_data[did] = r["metadata"]
    for rank, r in enumerate(graph_results):
        did = r.get("sql_id") or r.get("id") or r.get("doctor_id") or ""
        if not did:
            continue
        scores[did] = scores.get(did, 0) + 1 / (k + rank + 1)
        if did not in doc_data:
            doc_data[did] = r
        else:
            doc_data[did].update({k: v for k, v in r.items() if v})
    sorted_ids = sorted(scores, key=lambda x: scores[x], reverse=True)
    return [doc_data[did] for did in sorted_ids[:n] if did in doc_data]


def _build_context(fused: list[dict]) -> str | dict:
    if not fused:
        return {
            "context_text": f"No matching doctors found in {HOSPITAL_NAME} for this specialty.",
            "doctors": [],
            "found": False,
        }
    lines = [f"RELEVANT DOCTORS FROM {HOSPITAL_NAME.upper()}:\n"]
    for i, doc in enumerate(fused, 1):
        name  = doc.get("name", "Unknown")
        specs = doc.get("specializations", "")
        if isinstance(specs, list):
            specs = ", ".join(specs)
        exp   = doc.get("experience_years")
        desig = doc.get("designation", "Consultant")
        fee   = doc.get("consultation_fee")
        lines.append(f"{i}. {name}")
        lines.append(f"   Designation: {desig}")
        lines.append(f"   Specialization: {specs}")
        if exp:
            lines.append(f"   Experience: {exp} years")
        if fee:
            lines.append(f"   Consultation Fee: ₹{fee}")
        nxt = doc.get("next_slot")
        if nxt:
            lines.append(f"   Next available: {nxt['date']} {nxt['time']}")
        lines.append("")
    return "\n".join(lines)


def _no_language_response(language: str, specs: list[str]) -> dict:
    spec_label = ", ".join(specs) if specs else "this specialty"
    text = (
        f"No doctors who speak {language} are available"
        + (f" for {spec_label}" if specs else "")
        + f". Ask the patient whether they would like to be matched with doctors "
          f"in other languages (for example English or Hindi) before proceeding."
    )
    return {"context_text": text, "doctors": [], "needs_language_broadening": True}


def _no_language_for_specialty_response(language: str, specs: list[str], specialty_results: list[dict]) -> dict:
    avail = sorted({
        l for d in specialty_results
        for l in (d.get("languages", []) or [])
        if l and l.lower() != language.lower()
    })
    spec_label = ", ".join(specs)
    if avail:
        text = (
            f"No doctors who speak {language} are available for {spec_label}. "
            f"However, {spec_label} is available in these languages: {', '.join(avail)}. "
            f"Ask the patient if they would like to see options in those languages instead."
        )
    else:
        text = (
            f"No doctors who speak {language} are available for {spec_label}, "
            f"and this specialty is not listed under any other language in our records. "
            f"Ask the patient how they would like to proceed."
        )
    return {"context_text": text, "doctors": [], "needs_language_broadening": True}


def retrieve_context(query: str) -> dict:
    """Public API: retrieve doctors for a free-text patient query."""
    parsed = parse_query(query)
    specs  = resolve_specializations(parsed.get("specializations", []))

    specialty_results: list[dict] = []
    for spec in specs:
        specialty_results += find_doctors_by_specialization(spec, 8, TENANT_ID)

    graph_results: list[dict] = list(specialty_results)
    if parsed.get("doctor_name"):
        graph_results += find_by_fulltext(parsed["doctor_name"], 8, TENANT_ID)

    vector_results = semantic_search(query, 8, TENANT_ID)

    language = parsed.get("language")
    if language:
        lang_docs = find_doctors_by_language(language, 100, TENANT_ID)
        if not lang_docs:
            return _no_language_response(language, specs)

        lang_ids = {d.get("sql_id") or d.get("id") for d in lang_docs}
        specialty_lang_match = [
            d for d in specialty_results
            if (d.get("sql_id") or d.get("id")) in lang_ids
        ]
        if specs and specialty_results and not specialty_lang_match:
            return _no_language_for_specialty_response(language, specs, specialty_results)

        if graph_results:
            graph_results = [d for d in graph_results if (d.get("sql_id") or d.get("id")) in lang_ids]
        if vector_results:
            vector_results = [v for v in vector_results
                              if (v.get("metadata", {}).get("sql_id")
                                  or v.get("metadata", {}).get("id")
                                  or v.get("id")) in lang_ids]
        if not graph_results and not vector_results:
            graph_results = lang_docs

    if not graph_results and not vector_results:
        graph_results += find_by_fulltext(query, 8, TENANT_ID)

    fused = _fuse_results(vector_results, graph_results)

    slot_map = next_available_slots([d.get("sql_id") or d.get("id") for d in fused])
    for d in fused:
        key = d.get("sql_id") or d.get("id")
        if key in slot_map:
            d["next_slot"] = slot_map[key]

    context_text = _build_context(fused)
    if isinstance(context_text, dict):
        return context_text
    return {"context_text": context_text, "doctors": fused}
