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


def list_hospitals(conn):
    """All hospitals — used by the local test interface's hospital picker so
    it never hardcodes which hospitals exist or what mode each one runs."""
    sql = "SELECT hospital_id, name, booking_mode FROM hospitals ORDER BY name"
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql)
        return cur.fetchall()


def get_live_hospital(conn):
    """The one hospital currently receiving messages on our single real
    WhatsApp Business number — see migrations/0007. Returns None if none
    is flagged live; callers must fail closed on None, never default to
    a particular hospital."""
    sql = """
        SELECT hospital_id, name, booking_mode, address, city
        FROM hospitals
        WHERE is_live_number = true
        LIMIT 1
    """
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql)
        return cur.fetchone()


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
        SELECT doctor_id, hospital_id, name, is_active, specialization,
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
    """Find a patient by requester phone (+ name/relation for family members).

    The phone number is the stable identity for 'self' — name is just an
    editable attribute, so a differently-stated name never creates a second
    self-record; the caller compares the returned name against what was
    given to detect a mismatch. Family members are still looked up by name
    + relation, since one phone can legitimately have several of those."""
    relation_norm = (relation or "self").strip().lower()
    conditions = ["requested_by_phone = %s", "hospital_id = %s"]
    params = [requester_phone, str(hospital_id)]

    if relation_norm == "self":
        conditions.append("LOWER(relation_to_requester) = 'self'")
    else:
        conditions += ["LOWER(name) = LOWER(%s)", "LOWER(relation_to_requester) = LOWER(%s)"]
        params += [patient_name.strip(), relation_norm]

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
    """Insert a new family member. Uses ON CONFLICT for safe re-delivery —
    'self' and family members are protected by two separate partial unique
    indexes (see migrations/0003), so the conflict target depends on which
    kind of record this is."""
    relation_norm = (relation or "self").strip().lower()
    if relation_norm == "self":
        conflict_clause = (
            "ON CONFLICT (hospital_id, requested_by_phone) "
            "WHERE LOWER(relation_to_requester) = 'self' DO NOTHING"
        )
    else:
        conflict_clause = (
            "ON CONFLICT (hospital_id, requested_by_phone, (LOWER(name)), (LOWER(relation_to_requester))) "
            "WHERE LOWER(relation_to_requester) <> 'self' DO NOTHING"
        )
    sql = f"""
        INSERT INTO patients
            (hospital_id, name, phone, age, location, diagnosis,
             requested_by_phone, relation_to_requester)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        {conflict_clause}
        RETURNING patient_id, hospital_id, name, phone, age, location, diagnosis,
                  requested_by_phone, relation_to_requester
    """
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql, (
            str(hospital_id), name.strip(), phone, age, location, diagnosis,
            requester_phone.strip(), relation_norm,
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


def list_active_appointments(conn, requester_phone, hospital_id, patient_name=None):
    """All active bookings for this requester, across their whole family —
    TOKEN-mode (WAITING tokens) UNION ALL SLOT-mode (SCHEDULED appointments).
    token_number/slot_time are typed NULLs on the side that doesn't apply,
    so both branches share one column list and numeric token ordering still
    works (a bare untyped NULL would break UNION type inference)."""
    token_conditions = ["p.requested_by_phone = %s", "p.hospital_id = %s", "t.status = 'WAITING'"]
    slot_conditions  = ["p.requested_by_phone = %s", "p.hospital_id = %s", "a.status = 'SCHEDULED'"]
    params = [requester_phone, str(hospital_id)]
    if patient_name:
        token_conditions.append("LOWER(p.name) = LOWER(%s)")
        slot_conditions.append("LOWER(p.name) = LOWER(%s)")
        params.append(patient_name.strip())
    params = params + params   # same filters, once per UNION branch

    sql = f"""
        SELECT patient_name, relation_to_requester, doctor_id, doctor_name,
               department, token_number, date, slot_time
        FROM (
            SELECT p.name AS patient_name, p.relation_to_requester,
                   d.doctor_id, d.name AS doctor_name, t.department,
                   t.token_number AS token_number, ds.date::text AS date,
                   NULL::text AS slot_time
            FROM tokens t
            JOIN patients p         ON t.patient_id  = p.patient_id
            JOIN doctors d          ON t.doctor_id   = d.doctor_id
            JOIN doctor_sessions ds ON t.session_id  = ds.session_id
            WHERE {" AND ".join(token_conditions)}

            UNION ALL

            SELECT p.name AS patient_name, p.relation_to_requester,
                   d.doctor_id, d.name AS doctor_name, a.department,
                   NULL::integer AS token_number, a.appointment_date::text AS date,
                   s.start_time::text AS slot_time
            FROM appointments a
            JOIN patients p ON a.patient_id = p.patient_id
            JOIN doctors d  ON a.doctor_id   = d.doctor_id
            JOIN slots s    ON a.slot_id     = s.slot_id
            WHERE {" AND ".join(slot_conditions)}
        ) combined
        ORDER BY date ASC, token_number ASC NULLS LAST, slot_time ASC NULLS LAST
    """
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql, tuple(params))
        return cur.fetchall()


def cancel_token(conn, token_id):
    sql = "UPDATE tokens SET status = 'CANCELLED' WHERE token_id = %s"
    with conn.cursor() as cur:
        cur.execute(sql, (str(token_id),))


def shift_session_start(conn, session_id: str, delay_minutes: int):
    sql = """
        UPDATE doctor_sessions
        SET started_at = COALESCE(started_at, NOW()) + (%s || ' minutes')::INTERVAL
        WHERE session_id = %s
    """
    with conn.cursor() as cur:
        cur.execute(sql, (str(delay_minutes), str(session_id)))


# ═══════════════════════════════════════════════════════════════════════
# SLOT-mode functions — mirror the TOKEN-mode functions above exactly in
# style (RealDictCursor, parameterized queries); slots/appointments are the
# SLOT-mode equivalents of doctor_sessions/tokens (see the comparison table
# in docs/slot-based-appointment-implementation-guide.md §1).
# ═══════════════════════════════════════════════════════════════════════

def find_available_slots(conn, doctor_id, hospital_id, date, limit=5, offset=0):
    """Page through a doctor's AVAILABLE slots on a given date, future-only
    (excludes today's slots whose start_time has already passed).

    Returns (rows, has_more) — fetches one extra row beyond `limit` to
    detect whether more exist past this page, instead of a separate COUNT
    query. `offset` is the pagination cursor: 0 for the first page, then
    whatever this call's returned row count was, so a caller can always
    ask for "the next page" without needing to know the total up front."""
    sql = """
        SELECT slot_id, date::text AS date, start_time::text AS start_time,
               end_time::text AS end_time
        FROM slots
        WHERE doctor_id = %s AND hospital_id = %s AND date = %s
          AND status = 'AVAILABLE'
          AND (date > CURRENT_DATE OR (date = CURRENT_DATE AND start_time > CURRENT_TIME))
        ORDER BY start_time
        LIMIT %s OFFSET %s
    """
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql, (str(doctor_id), str(hospital_id), date, limit + 1, offset))
        rows = cur.fetchall()
    has_more = len(rows) > limit
    return rows[:limit], has_more


def lock_slot(conn, slot_id, hospital_id):
    """SELECT ... FOR UPDATE, hospital-scoped — the multi-tenant boundary
    for slot booking. Must run inside the same transaction as the write
    that follows it (mark_slot_booked/insert_appointment or the reschedule
    equivalent)."""
    sql = """
        SELECT slot_id, hospital_id, doctor_id, date, start_time, end_time, status
        FROM slots
        WHERE slot_id = %s AND hospital_id = %s
        FOR UPDATE
    """
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql, (str(slot_id), str(hospital_id)))
        return cur.fetchone()


def mark_slot_booked(conn, slot_id):
    sql = "UPDATE slots SET status = 'BOOKED', updated_at = NOW() WHERE slot_id = %s AND status = 'AVAILABLE'"
    with conn.cursor() as cur:
        cur.execute(sql, (str(slot_id),))


def mark_slot_available(conn, slot_id):
    """Guarded by status='BOOKED' so a stale/racing call can't free a slot
    that's already been cancelled or reassigned by another path — rowcount
    0 just means it was already free, not an error."""
    sql = "UPDATE slots SET status = 'AVAILABLE', updated_at = NOW() WHERE slot_id = %s AND status = 'BOOKED'"
    with conn.cursor() as cur:
        cur.execute(sql, (str(slot_id),))


def insert_appointment(conn, hospital_id, patient_id, doctor_id, slot_id, department, appointment_date):
    sql = """
        INSERT INTO appointments
            (hospital_id, patient_id, doctor_id, slot_id, department, appointment_date, status)
        VALUES (%s, %s, %s, %s, %s, %s, 'SCHEDULED')
        RETURNING appointment_id, hospital_id, patient_id, doctor_id, slot_id,
                  department, appointment_date::text AS appointment_date, status
    """
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql, (
            str(hospital_id), str(patient_id), str(doctor_id),
            str(slot_id), department, appointment_date,
        ))
        return cur.fetchone()


def find_active_appointments(conn, patient_id, doctor_id, date=None):
    """Slot-mode equivalent of find_active_token — returns the *set* of
    matches (plural), not just one, so callers (book_slot's duplicate
    check, reschedule_slot's disambiguation) can tell zero/one/many apart
    themselves rather than this function guessing which one is meant."""
    conditions = ["a.patient_id = %s", "a.doctor_id = %s", "a.status = 'SCHEDULED'"]
    params = [str(patient_id), str(doctor_id)]
    if date:
        conditions.append("a.appointment_date = %s")
        params.append(date)

    sql = f"""
        SELECT a.appointment_id, a.patient_id, a.doctor_id, a.hospital_id, a.slot_id,
               a.department, a.appointment_date::text AS date, a.status,
               d.name AS doctor_name, s.start_time::text AS start_time, s.end_time::text AS end_time
        FROM appointments a
        JOIN doctors d ON a.doctor_id = d.doctor_id
        JOIN slots s   ON a.slot_id   = s.slot_id
        WHERE {" AND ".join(conditions)}
        ORDER BY a.appointment_date, s.start_time
    """
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql, tuple(params))
        return cur.fetchall()


def cancel_appointment_row(conn, appointment_id):
    """Returns the cancelled row (with its slot_id) so the caller frees the
    right slot without trusting a separately-supplied slot_id argument."""
    sql = """
        UPDATE appointments SET status = 'CANCELLED', cancelled_at = NOW()
        WHERE appointment_id = %s AND status = 'SCHEDULED'
        RETURNING appointment_id, slot_id
    """
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql, (str(appointment_id),))
        return cur.fetchone()


def update_appointment_slot(conn, appointment_id, slot_id, appointment_date):
    sql = """
        UPDATE appointments
        SET slot_id = %s, appointment_date = %s, updated_at = NOW()
        WHERE appointment_id = %s AND status = 'SCHEDULED'
        RETURNING appointment_id, slot_id, appointment_date::text AS appointment_date
    """
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql, (str(slot_id), appointment_date, str(appointment_id)))
        return cur.fetchone()
