from fastapi import FastAPI, HTTPException, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session
from sqlalchemy import func as sqlfunc
from datetime import datetime, timedelta
import os

from backend.database import SessionLocal, engine
from backend.models import Base, User, ChatMessage, Account, Appointment
from backend.settings import APP_NAME, MAX_QUESTIONS
from backend.gemini import get_gemini_response
from backend.voice.router import router as voice_router
from backend import auth as auth_lib
from backend.store import save_appointment

# Create tables
Base.metadata.create_all(bind=engine)

app = FastAPI(title=APP_NAME)

# Live voice health assistant (WebSocket at /ws/voice)
app.include_router(voice_router)


# Seed a demo dashboard account so clients can log in instantly.
def _seed_demo_account():
    db = SessionLocal()
    try:
        if not db.query(Account).filter(Account.email == "demo@shifa.ai").first():
            db.add(Account(
                name="Demo Clinic",
                email="demo@shifa.ai",
                password_hash=auth_lib.hash_password("demo1234"),
            ))
            db.commit()
    finally:
        db.close()


_seed_demo_account()

# CORS Middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# -------------------------
# Database Dependency
# -------------------------
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# -------------------------
# Schemas
# -------------------------
class UserCreate(BaseModel):
    name: str
    gender: str
    age: int


class ChatRequest(BaseModel):
    user_id: int
    message: str


# -------------------------
# Health Check
# -------------------------
@app.get("/api/health")
def health():
    return {"status": "ok", "app": APP_NAME}


# -------------------------
# Create User
# -------------------------
@app.post("/api/users/create")
def create_user(payload: UserCreate, db: Session = Depends(get_db)):
    # Check if user with same name already exists
    existing_user = db.query(User).filter(User.name == payload.name).first()
    if existing_user:
        raise HTTPException(
            status_code=400,
            detail="User already exists"
        )

    user = User(
        name=payload.name,
        gender=payload.gender,
        age=payload.age,
    )
    db.add(user)
    db.commit()
    db.refresh(user)

    return {"user_id": user.id}


# -------------------------
# Send Chat Message
# -------------------------
@app.post("/api/chat/send")
def send_chat(payload: ChatRequest, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.id == payload.user_id).first()

    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    if user.question_count >= MAX_QUESTIONS:
        raise HTTPException(
            status_code=403,
            detail="Question limit reached"
        )

    # Save user message
    user_message = ChatMessage(
        user_id=user.id,
        role="user",
        content=payload.message
    )
    db.add(user_message)

    # Get Gemini response
    ai_response_text = get_gemini_response(payload.message)

    ai_message = ChatMessage(
        user_id=user.id,
        role="assistant",
        content=ai_response_text
    )
    db.add(ai_message)

    # Increment question count
    user.question_count += 1

    db.commit()

    return {
        "reply": ai_response_text,
        "questions_used": user.question_count,
        "questions_left": MAX_QUESTIONS - user.question_count
    }


# -------------------------
# Get Chat History
# -------------------------
@app.get("/api/chat/history/{user_id}")
def chat_history(user_id: int, db: Session = Depends(get_db)):
    messages = (
        db.query(ChatMessage)
        .filter(ChatMessage.user_id == user_id)
        .order_by(ChatMessage.created_at)
        .all()
    )

    return [
        {
            "role": msg.role,
            "content": msg.content,
            "created_at": msg.created_at
        }
        for msg in messages
    ]

# -------------------------
# Admin Routes
# -------------------------
@app.get("/api/admin/users")
def get_all_users(db: Session = Depends(get_db)):
    users = db.query(User).all()
    return [
        {
            "id": u.id,
            "name": u.name,
            "gender": u.gender,
            "age": u.age,
            "question_count": u.question_count,
            "created_at": u.created_at
        }
        for u in users
    ]

@app.delete("/api/admin/users/{user_id}")
def delete_user(user_id: int, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    
    # Delete related chat messages first
    db.query(ChatMessage).filter(ChatMessage.user_id == user_id).delete()
    
    db.delete(user)
    db.commit()
    return {"status": "success", "message": f"User {user_id} deleted"}

# -------------------------
# Auth (dashboard accounts)
# -------------------------
class SignupIn(BaseModel):
    name: str
    email: str
    password: str


class LoginIn(BaseModel):
    email: str
    password: str


@app.post("/api/auth/signup")
def signup(payload: SignupIn, db: Session = Depends(get_db)):
    email = payload.email.strip().lower()
    if db.query(Account).filter(Account.email == email).first():
        raise HTTPException(status_code=400, detail="An account with this email already exists.")
    account = Account(
        name=payload.name.strip() or "My Business",
        email=email,
        password_hash=auth_lib.hash_password(payload.password),
    )
    db.add(account)
    db.commit()
    db.refresh(account)
    token = auth_lib.create_token(account.id, email)
    return {"token": token, "name": account.name, "email": account.email}


@app.post("/api/auth/login")
def login(payload: LoginIn, db: Session = Depends(get_db)):
    email = payload.email.strip().lower()
    account = db.query(Account).filter(Account.email == email).first()
    if not account or not auth_lib.verify_password(payload.password, account.password_hash):
        raise HTTPException(status_code=401, detail="Invalid email or password.")
    token = auth_lib.create_token(account.id, email)
    return {"token": token, "name": account.name, "email": account.email}


# -------------------------
# Appointments (dashboard)
# -------------------------
class AppointmentIn(BaseModel):
    patient_name: str
    reason: str = "General consultation"
    date_text: str = ""
    time_text: str = ""


def _appt_dict(a: Appointment):
    return {
        "id": a.id,
        "reference": a.reference,
        "patient_name": a.patient_name,
        "reason": a.reason,
        "date_text": a.date_text,
        "time_text": a.time_text,
        "status": a.status,
        "source": a.source,
        "created_at": a.created_at,
    }


@app.get("/api/appointments")
def list_appointments(db: Session = Depends(get_db), _=Depends(auth_lib.get_current_account)):
    rows = db.query(Appointment).order_by(Appointment.created_at.desc()).all()
    return [_appt_dict(a) for a in rows]


@app.post("/api/appointments")
def create_appointment_api(payload: AppointmentIn, db: Session = Depends(get_db),
                           _=Depends(auth_lib.get_current_account)):
    import random, string
    ref = "SH-" + "".join(random.choices(string.ascii_uppercase + string.digits, k=6))
    appt = Appointment(
        reference=ref, patient_name=payload.patient_name, reason=payload.reason,
        date_text=payload.date_text, time_text=payload.time_text, source="manual", status="confirmed",
    )
    db.add(appt)
    db.commit()
    db.refresh(appt)
    return _appt_dict(appt)


@app.patch("/api/appointments/{appt_id}")
def update_appointment(appt_id: int, status: str, db: Session = Depends(get_db),
                       _=Depends(auth_lib.get_current_account)):
    appt = db.query(Appointment).filter(Appointment.id == appt_id).first()
    if not appt:
        raise HTTPException(status_code=404, detail="Appointment not found")
    if status not in ("confirmed", "completed", "cancelled"):
        raise HTTPException(status_code=400, detail="Invalid status")
    appt.status = status
    db.commit()
    return _appt_dict(appt)


@app.delete("/api/appointments/{appt_id}")
def delete_appointment(appt_id: int, db: Session = Depends(get_db),
                       _=Depends(auth_lib.get_current_account)):
    appt = db.query(Appointment).filter(Appointment.id == appt_id).first()
    if not appt:
        raise HTTPException(status_code=404, detail="Appointment not found")
    db.delete(appt)
    db.commit()
    return {"status": "deleted"}


# -------------------------
# Dashboard stats / analytics
# -------------------------
@app.get("/api/stats")
def stats(db: Session = Depends(get_db), _=Depends(auth_lib.get_current_account)):
    total_appts = db.query(Appointment).count()
    total_msgs = db.query(ChatMessage).count()
    total_patients = db.query(User).count()
    by_status = {s: db.query(Appointment).filter(Appointment.status == s).count()
                 for s in ("confirmed", "completed", "cancelled")}
    by_source = {s: db.query(Appointment).filter(Appointment.source == s).count()
                 for s in ("voice", "chat", "manual")}

    # Appointments per day for the last 7 days.
    today = datetime.utcnow().date()
    series = []
    for i in range(6, -1, -1):
        day = today - timedelta(days=i)
        start = datetime(day.year, day.month, day.day)
        end = start + timedelta(days=1)
        count = (db.query(Appointment)
                 .filter(Appointment.created_at >= start, Appointment.created_at < end)
                 .count())
        series.append({"date": day.strftime("%a"), "count": count})

    return {
        "total_appointments": total_appts,
        "total_messages": total_msgs,
        "total_patients": total_patients,
        "by_status": by_status,
        "by_source": by_source,
        "last_7_days": series,
    }


# -------------------------
# Serve Frontend
# -------------------------
# Mount assets first to avoid being shadowed by the root mount
app.mount("/assets", StaticFiles(directory="frontend/assets"), name="assets")

# Serve specific HTML files
@app.get("/")
def read_index():
    from fastapi.responses import FileResponse
    return FileResponse("frontend/index.html")

@app.get("/get-started")
def read_get_started():
    from fastapi.responses import FileResponse
    return FileResponse("frontend/get-started.html")


@app.get("/voice")
def read_voice():
    from fastapi.responses import FileResponse
    return FileResponse("frontend/voice.html")

@app.get("/chat")
def read_chat():
    from fastapi.responses import FileResponse
    return FileResponse("frontend/chat.html")

@app.get("/admin")
def read_admin():
    return FileResponse("frontend/admin.html")


@app.get("/login")
def read_login():
    return FileResponse("frontend/login.html")


@app.get("/signup")
def read_signup():
    return FileResponse("frontend/signup.html")


@app.get("/dashboard")
def read_dashboard():
    return FileResponse("frontend/dashboard.html")
