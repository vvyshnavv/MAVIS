import datetime
import hashlib
import logging
from sqlalchemy import select, delete
from sqlalchemy.ext.asyncio import AsyncSession
from backend.database.models import User, Doctor, Slot, Appointment, TriageNote, FollowUpRecord

logger = logging.getLogger("mavis.seed")

# Simple, reliable password hashing (SHA-256 with salt)
def hash_password(password: str) -> str:
    salt = "mavis_salt_2026"
    return hashlib.sha256((password + salt).encode("utf-8")).hexdigest()

def verify_password(password: str, hashed: str) -> bool:
    return hash_password(password) == hashed

async def seed_database(db: AsyncSession):
    """
    Ensures starter accounts and appointment availability exist without creating sample patient records.
    """
    # 1. Admin User
    admin_check = await db.execute(select(User).where(User.email == "admin@mavis.hospital"))
    admin_user = admin_check.scalars().first()
    if not admin_user:
        admin_user = User(
            email="admin@mavis.hospital",
            hashed_password=hash_password("admin123"),
            full_name="Hospital Director (Admin)",
            role="ADMIN",
            is_active=True
        )
        db.add(admin_user)
        await db.flush()

    # 2. Doctor Users & Doctor Records: Exactly 6 Doctors
    # Dr. A (Ophthalmology), Dr. B (Cardiology), Dr. C (Dermatology), Dr. D (Orthopedics), Dr. E (ENT), Dr. F (General Medicine)
    doctors_info = [
        {"name": "Dr. A", "email": "dr.a@mavis.hospital", "dept": "Ophthalmology", "spec": "Ophthalmology & Refractive Surgery", "room": "OPD-101"},
        {"name": "Dr. B", "email": "dr.b@mavis.hospital", "dept": "Cardiology", "spec": "Interventional Cardiology", "room": "OPD-102"},
        {"name": "Dr. C", "email": "dr.c@mavis.hospital", "dept": "Dermatology", "spec": "Clinical & Cosmetic Dermatology", "room": "OPD-103"},
        {"name": "Dr. D", "email": "dr.d@mavis.hospital", "dept": "Orthopedics", "spec": "Joint Reconstruction & Trauma", "room": "OPD-104"},
        {"name": "Dr. E", "email": "dr.e@mavis.hospital", "dept": "ENT", "spec": "Otorhinolaryngology & Sinus Care", "room": "OPD-105"},
        {"name": "Dr. F", "email": "dr.f@mavis.hospital", "dept": "General Medicine", "spec": "Internal & General Medicine", "room": "OPD-106"},
    ]

    doctor_entities = []
    for doc in doctors_info:
        user_check = await db.execute(select(User).where(User.email == doc["email"]))
        user = user_check.scalars().first()
        if not user:
            user = User(
                email=doc["email"],
                hashed_password=hash_password("doctor123"),
                full_name=doc["name"],
                role="DOCTOR",
                is_active=True
            )
            db.add(user)
            await db.flush()

        doc_check = await db.execute(select(Doctor).where(Doctor.name == doc["name"]))
        doctor = doc_check.scalars().first()
        if not doctor:
            doctor = Doctor(
                user_id=user.id,
                name=doc["name"],
                department=doc["dept"],
                specialization=doc["spec"],
                room_number=doc["room"],
                is_active=True
            )
            db.add(doctor)
            await db.flush()

        doctor_entities.append(doctor)

    # 3. Create Slots for each Doctor (Today and Tomorrow)
    now = datetime.datetime.now(datetime.timezone.utc)
    base_date = now.replace(minute=0, second=0, microsecond=0)

    slot_hours = [9, 10, 11, 12, 14, 15, 16, 17]
    for doctor in doctor_entities:
        for day_offset in [0, 1]:
            target_date = base_date + datetime.timedelta(days=day_offset)
            for hour in slot_hours:
                slot_time = target_date.replace(hour=hour, minute=0)
                slot_check = await db.execute(
                    select(Slot).where(Slot.doctor_id == doctor.id, Slot.slot_timestamp == slot_time)
                )
                slot = slot_check.scalars().first()
                if not slot:
                    slot = Slot(
                        doctor_id=doctor.id,
                        slot_timestamp=slot_time,
                        is_booked=False
                    )
                    db.add(slot)

    await db.flush()

    seeded_appointments = (
        await db.execute(
            select(Appointment).where(Appointment.call_id.like("seed-call-%"))
        )
    ).scalars().all()
    if seeded_appointments:
        appointment_ids = [appointment.id for appointment in seeded_appointments]
        slot_ids = [appointment.slot_id for appointment in seeded_appointments]
        await db.execute(
            delete(FollowUpRecord).where(FollowUpRecord.appointment_id.in_(appointment_ids))
        )
        await db.execute(
            delete(TriageNote).where(TriageNote.appointment_id.in_(appointment_ids))
        )
        await db.execute(
            delete(Appointment).where(Appointment.id.in_(appointment_ids))
        )
        for slot_id in slot_ids:
            slot = await db.get(Slot, slot_id)
            if slot:
                slot.is_booked = False
        logger.info("Removed %d legacy seeded appointment(s).", len(seeded_appointments))

    await db.commit()
    logger.info("Starter accounts and appointment availability are ready.")
