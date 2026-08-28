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
                                     "description": "Patient's age. Required when action is BOOK; not needed for CANCEL."},
                "patient_location": {"type": "string",
                                     "description": "Patient's city or location. Required when action is BOOK; not needed for CANCEL."},
                "doctor_name":      {"type": "string",
                                     "description": "Doctor's display name as shown to the patient (optional, used in confirmation message)"},
                "symptoms":         {"type": "string",
                                     "description": "Patient's symptoms (optional)"},
                "date":             {"type": "string",
                                     "description": "Appointment date in YYYY-MM-DD format (optional, defaults to today)"},
                "relation_to_requester": {"type": "string",
                                     "description": "Who is this for? 'self' if for the patient themselves, otherwise their relation e.g. 'wife', 'father', 'son'. Default: 'self'."},
                "patient_phone":    {"type": "string",
                                     "description": "Only if the family member has their own separate phone number. Omit if same as the WhatsApp sender's number."},
            },
            "required": ["action", "doctor_id", "department", "patient_name"],
        },
    },
}

_list_appointments_schema = {
    "type": "function",
    "function": {
        "name": "list_appointments",
        "description": (
            "List the patient's active/upcoming bookings (across their whole family, "
            "since one WhatsApp number can book for several people). Use this whenever "
            "asked 'what are my appointments' — also use it BEFORE a cancel request, to "
            "find the doctor_id yourself instead of asking the patient to recall it."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "patient_name": {"type": "string",
                    "description": "Optional — only set this to filter to one specific family member, e.g. 'just my wife's appointment'."},
            },
            "required": [],
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

_memory_tool_schema = {
    "type": "function",
    "function": {
        "name": "memory_tool",
        "description": (
            "Fetch the patient's own profile, registered family members, and their "
            "recent or active appointments from the database. Call this when you need "
            "to know the patient's name, age, location, or family member details that "
            "weren't already given in the session context."
        ),
        "parameters": {
            "type": "object",
            "properties": {},
            "required": [],
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

PATIENT_TOOLS        = [_appointment_schema, _list_appointments_schema, _kg_retriever_schema, _memory_tool_schema]
PATIENT_TOOLS_WARMUP = [_list_appointments_schema, _kg_retriever_schema, _memory_tool_schema]
DOCTOR_TOOLS         = [_kg_retriever_schema, _query_data_schema]

ROLE_PERMISSIONS = {
    "appointment":       {Role.PATIENT},
    "list_appointments": {Role.PATIENT},
    "memory_tool":       {Role.PATIENT},
    "query_data":        {Role.DOCTOR},
    "kg_retriever":      {Role.PATIENT, Role.DOCTOR},
}
