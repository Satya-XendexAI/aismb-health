from models.session import Role

_get_session_impact_schema = {
    "type": "function",
    "function": {
        "name": "get_session_impact",
        "description": (
            "Get all waiting patients for a doctor's session on a given date. "
            "Call this immediately when an admin reports a doctor is running late, "
            "to see who is affected and their outstation/elderly status."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "doctor_id": {"type": "string", "description": "Doctor's sql_id"},
                "date":      {"type": "string", "description": "Date in YYYY-MM-DD format"},
            },
            "required": ["doctor_id", "date"],
        },
    },
}

_find_available_doctors_schema = {
    "type": "function",
    "function": {
        "name": "find_available_doctors",
        "description": (
            "Find alternative doctors with open sessions for a given specialization and date. "
            "Call this after get_session_impact to find reassignment candidates."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "specialization": {"type": "string", "description": "Department or specialization name"},
                "date":           {"type": "string", "description": "Date in YYYY-MM-DD format"},
            },
            "required": ["specialization", "date"],
        },
    },
}

_execute_plan_schema = {
    "type": "function",
    "function": {
        "name": "execute_plan",
        "description": (
            "Propose a multi-patient action plan for admin approval. "
            "Call after reasoning over affected patients. "
            "Each action specifies what happens to one patient's token."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "actions": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "action_type":            {"type": "string", "enum": ["REASSIGN", "SHIFT", "RETAIN"]},
                            "token_id":               {"type": "string"},
                            "patient_name":           {"type": "string"},
                            "patient_phone":          {"type": "string"},
                            "doctor_name":            {"type": "string"},
                            "new_doctor_id":          {"type": "string"},
                            "new_doctor_name":        {"type": "string"},
                            "new_session_id":         {"type": "string"},
                            "session_id":             {"type": "string"},
                            "delay_minutes":          {"type": "integer"},
                            "notification_message":   {"type": "string"},
                        },
                        "required": [
                            "action_type", "token_id", "patient_name",
                            "patient_phone", "doctor_name", "notification_message",
                        ],
                    },
                },
                "summary": {
                    "type": "string",
                    "description": "One-line human-readable description, e.g. '14 patients affected by Dr. Sharma delay'",
                },
            },
            "required": ["actions", "summary"],
        },
    },
}

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
                                     "description": "Appointment date in YYYY-MM-DD format. Required — always ask the patient which date they want, never assume today."},
                "relation_to_requester": {"type": "string",
                                     "description": "Who is this for? 'self' if for the patient themselves, otherwise their relation e.g. 'wife', 'father', 'son'. Default: 'self'."},
                "patient_phone":    {"type": "string",
                                     "description": "Only if the family member has their own separate phone number. Omit if same as the WhatsApp sender's number."},
            },
            "required": ["action", "doctor_id", "department", "patient_name", "date"],
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

_report_delay_schema = {
    "type": "function",
    "function": {
        "name": "report_delay",
        "description": (
            "Report that you (the doctor) will be late and notify all your waiting patients "
            "with updated estimated times. Call this as soon as the doctor mentions a delay — "
            "no other info needed. The system will show a preview for confirmation before sending."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "delay_minutes": {
                    "type": "integer",
                    "description": "Number of minutes the doctor will be delayed (e.g. 30, 60)",
                },
            },
            "required": ["delay_minutes"],
        },
    },
}

PATIENT_TOOLS        = [_appointment_schema, _list_appointments_schema, _kg_retriever_schema, _memory_tool_schema]
PATIENT_TOOLS_WARMUP = [_list_appointments_schema, _kg_retriever_schema, _memory_tool_schema]
DOCTOR_TOOLS         = [_kg_retriever_schema, _query_data_schema, _report_delay_schema]
ADMIN_TOOLS          = [_kg_retriever_schema, _get_session_impact_schema, _find_available_doctors_schema, _execute_plan_schema, _query_data_schema]

ROLE_PERMISSIONS = {
    "appointment":          {Role.PATIENT},
    "list_appointments":    {Role.PATIENT},
    "memory_tool":          {Role.PATIENT},
    "query_data":           {Role.DOCTOR, Role.ADMIN},
    "kg_retriever":         {Role.PATIENT, Role.DOCTOR, Role.ADMIN},
    "get_session_impact":   {Role.ADMIN},
    "find_available_doctors": {Role.ADMIN},
    "execute_plan":         {Role.ADMIN},
    "report_delay":         {Role.DOCTOR},
}
