import logging
import threading
import uuid
from dataclasses import dataclass, field
from typing import List, Literal, Tuple

from models.session import PlanAction

logger = logging.getLogger(__name__)

_jobs: dict[str, "JobStatus"] = {}


@dataclass
class JobStatus:
    job_id:  str
    status:  Literal["RUNNING", "COMPLETE", "PARTIAL"]
    total:   int
    sent:    int = 0
    failed:  int = 0
    errors:  List[Tuple[str, str]] = field(default_factory=list)


def get_job_status(job_id: str) -> JobStatus | None:
    return _jobs.get(job_id)


def notify_patients_bulk(
    actions: List[PlanAction],
    notifier,
    skip_retain: bool = True,
) -> str:
    targets = [a for a in actions if not (skip_retain and a.action_type == "RETAIN")]
    job_id  = str(uuid.uuid4())
    job     = JobStatus(job_id=job_id, status="RUNNING", total=len(targets))
    _jobs[job_id] = job

    thread = threading.Thread(
        target=_send_loop,
        args=(job_id, targets, notifier),
        daemon=True,
    )
    thread.start()
    return job_id


def _send_loop(job_id: str, actions: List[PlanAction], notifier) -> None:
    job = _jobs[job_id]
    for action in actions:
        try:
            notifier.send(action.patient_phone, action.notification_message)
            job.sent += 1
        except Exception as exc:
            logger.warning("notify failed for %s: %s", action.patient_phone, exc)
            job.failed += 1
            job.errors.append((action.patient_phone, str(exc)))

    job.status = "COMPLETE" if job.failed == 0 else "PARTIAL"
