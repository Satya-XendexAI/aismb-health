"""Maps each hospital's Postgres hospital_id to its Neo4j tenant_id.

Booking (Postgres) and doctor search (kg_retriever -> Neo4j) grew as two
separate systems and ended up with two different identifier strings for
the same hospital in some cases (e.g. Chaitanya is "glngs-chn" in Postgres
but "glh-chn" in Neo4j — verified against the live graph). This is the one
place that bridges them, so kg_retriever never guesses or defaults.

hospital_id here always comes from the session (derived from the WhatsApp
number the message arrived on, or the local test interface's hospital
picker) — never from the LLM or the patient's own message.
"""

HOSPITAL_TO_KG_TENANT = {
    "glngs-chn": "glh-chn",   # Chaitanya Multi Speciality Hospital
    "mit-lbn":   "mit-lbn",   # Mithra Hospital, L.B. Nagar
}


def kg_tenant_id(hospital_id: str) -> str | None:
    """None means no mapping is known for this hospital yet — caller
    decides how to handle that (kg_retriever searches unscoped rather
    than silently defaulting to some other hospital's tenant)."""
    return HOSPITAL_TO_KG_TENANT.get(hospital_id)
