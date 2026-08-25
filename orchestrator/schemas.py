from models.session import Role

_appointment_schema = {
    "type": "function",
    "function": {
        "name": "appointment",
        "description": (
            "Book or cancel a token (queue-based) appointment for the patient. "
            "Always call kg_retriever first to get the doctor_id and department. "
            "Ask for patient_name before calling this tool if not already known."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action":           {"type": "string", "enum": ["BOOK", "CANCEL"],
                                     "description": "BOOK to book a new token, CANCEL to cancel existing"},
                "doctor_id":        {"type": "string",
                                     "description": "Doctor's ID from kg_retriever results (sql_id field)"},
                "department":       {"type": "string",
                                     "description": "Doctor's department or specialization"},
                "patient_name":     {"type": "string",
                                     "description": "Patient's full name"},
                "patient_age":      {"type": "integer",
                                     "description": "Patient's age (optional)"},
                "patient_location": {"type": "string",
                                     "description": "Patient's city or location (optional)"},
                "doctor_name":      {"type": "string",
                                     "description": "Doctor's display name as shown to the patient (optional, used in confirmation message)"},
                "symptoms":         {"type": "string",
                                     "description": "Patient's symptoms (optional)"},
                "date":             {"type": "string",
                                     "description": "Appointment date in YYYY-MM-DD format (optional, defaults to today)"},
            },
            "required": ["action", "doctor_id", "department", "patient_name"],
        },
    },
}

_kg_retriever_schema = {
    "type": "function",
    "function": {
        "name": "kg_retriever",
        "description": "Find doctors by symptoms, specialization, name, language, or experience. Use for any query about finding or getting info on doctors.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "The user's query in their own words"},
            },
            "required": ["query"],
        },
    },
}

_query_data_schema = {
    "type": "function",
    "function": {
        "name": "query_data",
        "description": "Query your patients' appointment and token data using a natural language question.",
        "parameters": {
            "type": "object",
            "properties": {
                "question": {
                    "type": "string",
                    "description": "Natural language question about your patients or tokens",
                },
            },
            "required": ["question"],
        },
    },
}

PATIENT_TOOLS        = [_appointment_schema, _kg_retriever_schema]
PATIENT_TOOLS_WARMUP = [_kg_retriever_schema]   # appointment stripped for conversational warmup turns
DOCTOR_TOOLS         = [_kg_retriever_schema, _query_data_schema]

ROLE_PERMISSIONS = {
    "appointment":  {Role.PATIENT},
    "query_data":   {Role.DOCTOR},
    "kg_retriever": {Role.PATIENT, Role.DOCTOR},
}
