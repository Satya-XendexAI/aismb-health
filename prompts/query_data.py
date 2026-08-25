def sql_generate(question: str, schema_text: str, tables: list, doctor_name: str) -> str:
    return (
        f"You are a SQL assistant for a hospital database.\n"
        f"Generate a single read-only SELECT query to answer the question for doctor: {doctor_name}.\n\n"
        f"Rules:\n"
        f"- Only query these tables: {', '.join(tables)}\n"
        f"- Always filter by doctor_id = %(doctor_id)s (use this exact placeholder)\n"
        f"- Return only a SELECT statement — no INSERT, UPDATE, DELETE, DROP, or DDL\n"
        f"- Use standard PostgreSQL syntax\n"
        f"- Return only the SQL, no explanation, no markdown fences\n\n"
        f"Table schemas:\n{schema_text}\n\n"
        f"Question: {question}"
    )


def sql_validate(sql: str, schema_text: str) -> str:
    return (
        "You are a SQL validator for a hospital database.\n"
        "Given the table schemas below and a SQL query, check whether every table "
        "and every column referenced in the query actually exists in the schema.\n\n"
        "If the SQL is fully valid (all tables and columns exist), return it unchanged.\n"
        "If the SQL references any non-existent table or column, rewrite it using only "
        "the tables and columns that are listed in the schema.\n"
        "If the query cannot be fixed at all, return exactly: INVALID\n\n"
        "Rules:\n"
        "- Keep the WHERE doctor_id = %%(doctor_id)s filter exactly as-is\n"
        "- Return only the SQL query or INVALID — no explanation, no markdown fences\n\n"
        f"Schema:\n{schema_text}\n\n"
        f"SQL:\n{sql}"
    )
