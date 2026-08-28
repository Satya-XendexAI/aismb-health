import logging
from dataclasses import dataclass, field
from typing import List, Tuple

from models.session import PlanAction
from tools.appointment.database import get_connection, cancel_token, insert_token, shift_session_start

logger = logging.getLogger(__name__)


@dataclass
class BulkResult:
    succeeded:   List[PlanAction]
    failed:      List[Tuple[PlanAction, str]] = field(default_factory=list)
    rolled_back: bool = False


def bulk_reschedule(actions: List[PlanAction], hospital_id: str) -> BulkResult:
    succeeded: List[PlanAction] = []

    # Deduplicate SHIFT updates: (session_id, delay_minutes) pairs
    shift_pairs: dict[str, int] = {}
    for a in actions:
        if a.action_type == "SHIFT" and a.session_id and a.delay_minutes:
            shift_pairs[a.session_id] = a.delay_minutes

    try:
        with get_connection() as conn:
            for action in actions:
                if action.action_type == "RETAIN":
                    succeeded.append(action)

                elif action.action_type == "SHIFT":
                    # Already deduped — the shift_pairs loop will run below
                    succeeded.append(action)

                elif action.action_type == "REASSIGN":
                    if not action.new_session_id:
                        raise ValueError(
                            f"REASSIGN for {action.patient_name} missing new_session_id"
                        )
                    cancel_token(conn, action.token_id)

                    # We need patient_id and department from the original token
                    with conn.cursor() as cur:
                        cur.execute(
                            "SELECT patient_id, department FROM tokens WHERE token_id = %s",
                            (str(action.token_id),),
                        )
                        orig = cur.fetchone()
                    if orig is None:
                        raise ValueError(f"token_id {action.token_id} not found")

                    patient_id, department = orig

                    # Resolve doctor_id from the new session
                    with conn.cursor() as cur:
                        cur.execute(
                            "SELECT doctor_id FROM doctor_sessions WHERE session_id = %s",
                            (str(action.new_session_id),),
                        )
                        sess_row = cur.fetchone()
                    if sess_row is None:
                        raise ValueError(f"new_session_id {action.new_session_id} not found")
                    new_doctor_id = sess_row[0]

                    token_row = insert_token(
                        conn,
                        session_id=action.new_session_id,
                        patient_id=str(patient_id),
                        doctor_id=str(new_doctor_id),
                        hospital_id=str(hospital_id),
                        department=department,
                    )
                    action.new_token_number = token_row["token_number"]
                    succeeded.append(action)

            # Apply deduped SHIFT updates in a single pass
            for session_id, delay_minutes in shift_pairs.items():
                shift_session_start(conn, session_id, delay_minutes)

    except Exception as exc:
        logger.error("bulk_reschedule rolled back: %s", exc)
        return BulkResult(succeeded=[], rolled_back=True)

    return BulkResult(succeeded=succeeded, rolled_back=False)
