import os
import psycopg2
import psycopg2.extras
from contextlib import contextmanager
from dotenv import load_dotenv

load_dotenv()


@contextmanager
def get_connection():
    conn = psycopg2.connect(
        host=os.getenv("DB_HOST", "localhost"),
        port=int(os.getenv("DB_PORT", 5432)),
        dbname=os.getenv("DB_NAME", "postgres"),
        user=os.getenv("DB_USER", "postgres"),
        password=os.getenv("DB_PASSWORD", ""),
    )
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def get_hospital(conn, hospital_id):
    sql = """
        SELECT hospital_id, name, booking_mode, address, city
        FROM hospitals
        WHERE hospital_id = %s
    """
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql, (str(hospital_id),))
        return cur.fetchone()


def get_doctor(conn, doctor_id, hospital_id):
    sql = """
        SELECT doctor_id, hospital_id, name, is_active,
               avg_checkin_time, avg_consultation_minutes, fee
        FROM doctors
        WHERE doctor_id = %s
          AND hospital_id = %s
          AND is_active = true
    """
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql, (str(doctor_id), str(hospital_id)))
        return cur.fetchone()


def find_family_member(conn, requester_phone, hospital_id, patient_name, relation=None):
    """Find a patient by requester phone + name (+ optional relation)."""
    conditions = [
        "requested_by_phone = %s",
        "hospital_id = %s",
        "LOWER(name) = LOWER(%s)",
    ]
    params = [requester_phone, str(hospital_id), patient_name.strip()]

    if relation and relation.strip().lower() != "self":
        conditions.append("LOWER(relation_to_requester) = LOWER(%s)")
        params.append(relation.strip())

    sql = f"""
        SELECT patient_id, hospital_id, name, phone, age, location, diagnosis,
               requested_by_phone, relation_to_requester
        FROM patients
        WHERE {" AND ".join(conditions)}
        ORDER BY updated_at DESC
        LIMIT 1
    """
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql, tuple(params))
        return cur.fetchone()


def insert_family_member(conn, hospital_id, requester_phone, name, phone,
                         relation, age=None, location=None, diagnosis=None):
    """Insert a new family member. Uses ON CONFLICT for safe re-delivery."""
    sql = """
        INSERT INTO patients
            (hospital_id, name, phone, age, location, diagnosis,
             requested_by_phone, relation_to_requester)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (hospital_id, requested_by_phone, (LOWER(name))) DO NOTHING
        RETURNING patient_id, hospital_id, name, phone, age, location, diagnosis,
                  requested_by_phone, relation_to_requester
    """
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql, (
            str(hospital_id), name.strip(), phone, age, location, diagnosis,
            requester_phone.strip(), (relation or "self").strip().lower(),
        ))
        row = cur.fetchone()
        if row is None:
            return find_family_member(conn, requester_phone, hospital_id, name, relation)
        return row


def touch_family_member(conn, patient_id):
    """Update timestamp so recently-booked members appear first."""
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE patients SET updated_at = NOW() WHERE patient_id = %s",
            (str(patient_id),),
        )


def get_or_create_today_session(conn, doctor_id, hospital_id, date=None):
    if date:
        insert_sql = """
            INSERT INTO doctor_sessions (doctor_id, hospital_id, date, status)
            VALUES (%s, %s, %s, 'OPEN')
            ON CONFLICT (hospital_id, doctor_id, date) DO NOTHING
            RETURNING session_id, doctor_id, hospital_id, date, status, started_at
        """
        select_sql = """
            SELECT session_id, doctor_id, hospital_id, date, status, started_at
            FROM doctor_sessions
            WHERE doctor_id = %s AND hospital_id = %s AND date = %s
        """
        insert_params = (str(doctor_id), str(hospital_id), date)
        select_params = (str(doctor_id), str(hospital_id), date)
    else:
        insert_sql = """
            INSERT INTO doctor_sessions (doctor_id, hospital_id, date, status)
            VALUES (%s, %s, CURRENT_DATE, 'OPEN')
            ON CONFLICT (hospital_id, doctor_id, date) DO NOTHING
            RETURNING session_id, doctor_id, hospital_id, date, status, started_at
        """
        select_sql = """
            SELECT session_id, doctor_id, hospital_id, date, status, started_at
            FROM doctor_sessions
            WHERE doctor_id = %s AND hospital_id = %s AND date = CURRENT_DATE
        """
        insert_params = (str(doctor_id), str(hospital_id))
        select_params = (str(doctor_id), str(hospital_id))

    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(insert_sql, insert_params)
        row = cur.fetchone()
        if row is None:
            cur.execute(select_sql, select_params)
            row = cur.fetchone()
        return row


def insert_token(conn, session_id, patient_id, doctor_id, hospital_id, department):
    lock_sql        = "SELECT session_id FROM doctor_sessions WHERE session_id = %s FOR UPDATE"
    next_number_sql = "SELECT COALESCE(MAX(token_number), 0) + 1 AS next_number FROM tokens WHERE session_id = %s"
    insert_sql      = """
        INSERT INTO tokens (session_id, patient_id, doctor_id, hospital_id,
                            department, token_number, status)
        VALUES (%s, %s, %s, %s, %s, %s, 'WAITING')
        RETURNING token_id, session_id, patient_id, doctor_id, hospital_id,
                  department, token_number, status
    """
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(lock_sql, (str(session_id),))
        cur.execute(next_number_sql, (str(session_id),))
        next_number = cur.fetchone()["next_number"]
        cur.execute(insert_sql, (
            str(session_id), str(patient_id), str(doctor_id),
            str(hospital_id), department, next_number,
        ))
        return cur.fetchone()


def count_patients_ahead(conn, session_id, token_number):
    sql = """
        SELECT COUNT(*) AS count FROM tokens
        WHERE session_id = %s AND token_number < %s AND status = 'WAITING'
    """
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql, (str(session_id), token_number))
        return cur.fetchone()["count"]


def find_active_token(conn, patient_id, doctor_id, date=None):
    if date:
        sql = """
            SELECT t.token_id, t.session_id, t.patient_id, t.doctor_id,
                   t.hospital_id, t.department, t.token_number, t.status
            FROM tokens t
            JOIN doctor_sessions ds ON t.session_id = ds.session_id
            WHERE t.patient_id = %s AND t.doctor_id = %s
              AND t.status = 'WAITING' AND ds.date = %s
        """
        params = (str(patient_id), str(doctor_id), date)
    else:
        sql = """
            SELECT t.token_id, t.session_id, t.patient_id, t.doctor_id,
                   t.hospital_id, t.department, t.token_number, t.status
            FROM tokens t
            JOIN doctor_sessions ds ON t.session_id = ds.session_id
            WHERE t.patient_id = %s AND t.doctor_id = %s
              AND t.status = 'WAITING' AND ds.date = CURRENT_DATE
        """
        params = (str(patient_id), str(doctor_id))
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql, params)
        return cur.fetchone()


def cancel_token(conn, token_id):
    sql = "UPDATE tokens SET status = 'CANCELLED' WHERE token_id = %s"
    with conn.cursor() as cur:
        cur.execute(sql, (str(token_id),))
