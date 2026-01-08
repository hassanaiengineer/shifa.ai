import os
from dotenv import load_dotenv

load_dotenv()

APP_NAME = "shifa.ai"
MAX_QUESTIONS = 10

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

if not GEMINI_API_KEY:
    raise RuntimeError("GEMINI_API_KEY is not set in .env")
