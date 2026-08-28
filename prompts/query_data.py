ADMIN_TABLES = ["doctors", "doctor_sessions", "patients", "tokens"]


def sql_generate(question: str, schema_text: str, tables: list,
                 bind_param: str, extra_rules: list[str] | None = None) -> str:
    rules = [
        f"- Only query these tables: {', '.join(tables)}",
        f"- Always filter by {bind_param} = %({bind_param})s (use this exact placeholder)",
        "- Return only a SELECT statement — no INSERT, UPDATE, DELETE, DROP, or DDL",
        "- Use standard PostgreSQL syntax",
        "- Return only the SQL, no explanation, no markdown fences",
    ]
    if extra_rules:
        rules.extend(extra_rules)
    return (
        f"You are a SQL assistant for a hospital database.\n"
        f"Generate a single read-only SELECT query to answer the question.\n\n"
        f"Rules:\n"
        + "\n".join(rules)
        + f"\n\nTable schemas:\n{schema_text}\n\n"
        f"Question: {question}"
    )


def sql_validate(sql: str, schema_text: str, bind_param: str) -> str:
    return (
        "You are a SQL validator for a hospital database.\n"
        "Given the table schemas below and a SQL query, check whether every table "
        "and every column referenced in the query actually exists in the schema.\n\n"
        "If the SQL is fully valid (all tables and columns exist), return it unchanged.\n"
        "If the SQL references any non-existent table or column, rewrite it using only "
        "the tables and columns that are listed in the schema.\n"
        "If the query cannot be fixed at all, return exactly: INVALID\n\n"
        "Rules:\n"
        f"- Keep the WHERE {bind_param} = %({bind_param})s filter exactly as-is\n"
        "- Return only the SQL query or INVALID — no explanation, no markdown fences\n\n"
        f"Schema:\n{schema_text}\n\n"
        f"SQL:\n{sql}"
    )
