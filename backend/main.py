from fastapi import FastAPI, HTTPException, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqlalchemy.orm import Session
import os

from backend.database import SessionLocal, engine
from backend.models import Base, User, ChatMessage
from backend.settings import APP_NAME, MAX_QUESTIONS
from backend.gemini import get_gemini_response

# Create tables
Base.metadata.create_all(bind=engine)

app = FastAPI(title=APP_NAME)

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

@app.get("/chat")
def read_chat():
    from fastapi.responses import FileResponse
    return FileResponse("frontend/chat.html")

@app.get("/admin")
def read_admin():
    from fastapi.responses import FileResponse
    return FileResponse("frontend/admin.html")
