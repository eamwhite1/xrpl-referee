import os
from datetime import datetime
from fastapi import FastAPI, HTTPException, Header
from sqlalchemy import create_engine, Column, Integer, String, Float, DateTime
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker

# --- DATABASE SETUP ---
DATABASE_URL = os.getenv("DATABASE_URL")
if DATABASE_URL and DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

engine = create_engine(DATABASE_URL)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

# --- THE BLUEPRINT (Schema) ---
class RefereeLog(Base):
    __tablename__ = "referee_logs"
    id = Column(Integer, primary_key=True, index=True)
    payment_hash = Column(String, unique=True, index=True, nullable=False)
    sender_address = Column(String)
    amount_xrp = Column(Float)
    task_description = Column(String)
    ai_verdict = Column(String)
    created_at = Column(DateTime, default=datetime.utcnow)

# Create the table if it's not there
Base.metadata.create_all(bind=engine)

# --- API APP ---
app = FastAPI()

@app.get("/")
def home():
    return {"status": "Referee is live and connected to the database"}

# Next we will update your /evaluate route here...