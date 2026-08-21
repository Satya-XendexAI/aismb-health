"""
database.py — The only file that actually talks to the database.

In simple terms: every other file in this project asks THIS file to
fetch or save information. Nowhere else in the project touches the
database directly — that keeps all the database "know-how" in one place.

Tables used: hospitals, doctors, patients, doctor_sessions, tokens
"""

import psycopg2
import psycopg2.extras
from contextlib import contextmanager
from config import DB_HOST, DB_PORT, DB_NAME, DB_USER, DB_PASSWORD


# ═══════════════════════════════════════════════════════════════
# CONNECTION HANDLING
# ═══════════════════════════════════════════════════════════════

@contextmanager
def get_connection():
    """
    Opens a fresh line to the database for one request, and always
    tidies up afterwards: saves the changes if everything went well,
    undoes them if something failed, and closes the connection either way.
    """
    conn = psycopg2.connect(
        host=DB_HOST,
        port=DB_PORT,
        dbname=DB_NAME,
        user=DB_USER,
        password=DB_PASSWORD,
    )
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ═══════════════════════════════════════════════════════════════
# HOSPITAL OPERATIONS
# ═══════════════════════════════════════════════════════════════

def get_hospital(conn, hospital_id):
    """
    Looks up one hospital's details by its ID.
    Gives back nothing (None) if no such hospital exists.
    """
    sql = """
        SELECT hospital_id, name, booking_mode, address, city
        FROM hospitals
        WHERE hospital_id = %s
    """
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql, (str(hospital_id),))
        return cur.fetchone()


# ═══════════════════════════════════════════════════════════════
# DOCTOR OPERATIONS
# ═══════════════════════════════════════════════════════════════

def get_doctor(conn, doctor_id, hospital_id):
    """
    Looks up one doctor's details — but only counts it as found if that
    doctor is currently active and actually works at this hospital.
    """
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


# ═══════════════════════════════════════════════════════════════
# PATIENT OPERATIONS
# ═══════════════════════════════════════════════════════════════

def find_patient(conn, phone, hospital_id):
    """
    Checks whether a patient with this phone number has visited this
    hospital before. Gives back nothing (None) if they're new.
    """
    sql = """
        SELECT patient_id, hospital_id, name, phone, age, location, diagnosis
        FROM patients
        WHERE phone = %s
          AND hospital_id = %s
    """
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql, (phone, str(hospital_id)))
        return cur.fetchone()


def insert_patient(conn, hospital_id, name, phone, age, location, diagnosis):
    """
    Saves a brand-new patient's details, for the first time they book
    with this hospital. The database itself hands out their patient ID.
    """
    sql = """
        INSERT INTO patients (hospital_id, name, phone, age, location, diagnosis)
        VALUES (%s, %s, %s, %s, %s, %s)
        RETURNING patient_id, hospital_id, name, phone, age, location, diagnosis
    """
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql, (
            str(hospital_id), name, phone, age, location, diagnosis
        ))
        return cur.fetchone()


# ═══════════════════════════════════════════════════════════════
# DOCTOR SESSION OPERATIONS (TOKEN mode)
# ═══════════════════════════════════════════════════════════════

def get_or_create_today_session(conn, doctor_id, hospital_id, date=None):
    """
    Finds the queue for this doctor on the given date (defaults to today),
    creating it if it doesn't exist yet.
    """
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


# ═══════════════════════════════════════════════════════════════
# TOKEN OPERATIONS
# ═══════════════════════════════════════════════════════════════

def insert_token(conn, session_id, patient_id, doctor_id, hospital_id, department):
    """
    Gives a patient the next token number in today's queue for this
    doctor, and saves it. Handles the case where two patients try to
    book at the exact same moment, so nobody ever gets a duplicate number.
    """
    # Step 1: Lock the session row to prevent concurrent inserts
    lock_sql = """
        SELECT session_id FROM doctor_sessions
        WHERE session_id = %s
        FOR UPDATE
    """
    # Step 2: Get the next token number
    next_number_sql = """
        SELECT COALESCE(MAX(token_number), 0) + 1 AS next_number
        FROM tokens
        WHERE session_id = %s
    """
    # Step 3: Insert the token
    insert_sql = """
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
            str(hospital_id), department, next_number
        ))
        return cur.fetchone()


def count_patients_ahead(conn, session_id, token_number):
    """
    Counts how many patients are still waiting ahead of this token,
    in today's queue for this doctor.
    """
    sql = """
        SELECT COUNT(*) AS count
        FROM tokens
        WHERE session_id = %s
          AND token_number < %s
          AND status = 'WAITING'
    """
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql, (str(session_id), token_number))
        return cur.fetchone()["count"]


def find_active_token(conn, patient_id, doctor_id, date=None):
    """
    Checks whether this patient already has a waiting token with this doctor
    on the given date (defaults to today).
    """
    if date:
        sql = """
            SELECT t.token_id, t.session_id, t.patient_id, t.doctor_id,
                   t.hospital_id, t.department, t.token_number, t.status
            FROM tokens t
            JOIN doctor_sessions ds ON t.session_id = ds.session_id
            WHERE t.patient_id = %s
              AND t.doctor_id = %s
              AND t.status = 'WAITING'
              AND ds.date = %s
        """
        params = (str(patient_id), str(doctor_id), date)
    else:
        sql = """
            SELECT t.token_id, t.session_id, t.patient_id, t.doctor_id,
                   t.hospital_id, t.department, t.token_number, t.status
            FROM tokens t
            JOIN doctor_sessions ds ON t.session_id = ds.session_id
            WHERE t.patient_id = %s
              AND t.doctor_id = %s
              AND t.status = 'WAITING'
              AND ds.date = CURRENT_DATE
        """
        params = (str(patient_id), str(doctor_id))

    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql, params)
        return cur.fetchone()


def cancel_token(conn, token_id):
    """
    Marks a token as cancelled, so it stops counting toward the queue.
    """
    sql = """
        UPDATE tokens
        SET status = 'CANCELLED'
        WHERE token_id = %s
    """
    with conn.cursor() as cur:
        cur.execute(sql, (str(token_id),))
