from typing import Optional
from models.session import Session, Role


class InMemoryRepository:
    """Stores sessions in memory. doctors is a list of doctor config dicts."""

    def __init__(self, doctors: list = None):
        self._sessions: dict = {}
        self._doctors:  list = doctors or []

    def get_session(self, hospital_id: str, from_number: str) -> Optional[Session]:
        return self._sessions.get(f"{hospital_id}:{from_number}")

    def save_session(self, session: Session):
        self._sessions[f"{session.hospital_id}:{session.from_number}"] = session

    def get_role(self, from_number: str) -> Role:
        return Role.DOCTOR if any(d["phone"] == from_number for d in self._doctors) else Role.PATIENT

    def get_doctor_config(self, from_number: str) -> dict | None:
        return next((d for d in self._doctors if d["phone"] == from_number), None)
