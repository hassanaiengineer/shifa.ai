from sqlalchemy import Column, Integer, String, Text, DateTime, ForeignKey
from sqlalchemy.sql import func
from backend.database import Base


class User(Base):
    """A patient / end-user of the assistant (onboarding + chat)."""

    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(100))
    gender = Column(String(50))
    age = Column(Integer)
    question_count = Column(Integer, default=0)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class ChatMessage(Base):
    __tablename__ = "chat_messages"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"))
    role = Column(String(20))  # user | assistant
    content = Column(Text)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class Account(Base):
    """A business/admin account that logs into the SaaS dashboard."""

    __tablename__ = "accounts"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(120))
    email = Column(String(200), unique=True, index=True)
    password_hash = Column(String(255))
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class Appointment(Base):
    """An appointment booked via the voice assistant, chatbot, or manually."""

    __tablename__ = "appointments"

    id = Column(Integer, primary_key=True, index=True)
    reference = Column(String(20), unique=True, index=True)
    patient_name = Column(String(120))
    reason = Column(String(200))
    date_text = Column(String(120))
    time_text = Column(String(60))
    status = Column(String(20), default="confirmed")  # confirmed | completed | cancelled
    source = Column(String(20), default="voice")  # voice | chat | manual
    created_at = Column(DateTime(timezone=True), server_default=func.now())
