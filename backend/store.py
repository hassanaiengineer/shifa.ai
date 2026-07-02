#
# Small persistence helpers shared by the API and the voice assistant.
#

from backend.database import SessionLocal
from backend.models import Appointment


def save_appointment(
    reference: str,
    patient_name: str | None,
    reason: str | None,
    date_text: str | None,
    time_text: str | None,
    source: str = "voice",
) -> None:
    """Insert an appointment row. Safe to call from anywhere (opens its own session)."""
    db = SessionLocal()
    try:
        appt = Appointment(
            reference=reference,
            patient_name=patient_name or "Guest",
            reason=reason or "General consultation",
            date_text=date_text or "",
            time_text=time_text or "",
            source=source,
            status="confirmed",
        )
        db.add(appt)
        db.commit()
    finally:
        db.close()
