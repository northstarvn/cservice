from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from app import deps, models
from app.schemas.chat import ChatMessageIn, ChatMessageOut, Sentiment, ChatHistoryCreate, ChatHistoryOut
from app.schemas.schemas import UserOut
from fastapi.responses import JSONResponse
import requests,os
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

router = APIRouter()

HF_SENTIMENT_URL = "https://api-inference.huggingface.co/models/distilbert-base-uncased-finetuned-sst-2-english"
HF_API_TOKEN = os.getenv("HF_API_TOKEN")

def analyze_sentiment(text: str):
    headers = {"Authorization": f"Bearer {HF_API_TOKEN}"} if HF_API_TOKEN else {}
    try:
        response = requests.post(HF_SENTIMENT_URL, headers=headers, json={"inputs": text}, timeout=10)
        response.raise_for_status()
        result = response.json()
        label = result[0][0]["label"].lower()
        score = result[0][0]["score"]
        return Sentiment(label=label, score=score)
    except Exception:
        return None

@router.post("/chat", response_model=ChatMessageOut)
async def chat_message(
    payload: ChatMessageIn,
    db: AsyncSession = Depends(deps.get_db),
    current_user: models.User = Depends(deps.get_current_user)
):
    user_message = payload.message
    if not user_message:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Message required"
        )

    ai_response = f"You said: '{user_message}'. How else can I help you?"

    sentiment = analyze_sentiment(user_message)

    suggestions = [
        "Tell me a joke",
        "Translate to French",
        "What's the weather?"
    ]

    vector = [0.1, 0.2, 0.3]

    # Save chat history
    chat_entry = models.ChatHistory(
        user_id=current_user.id if current_user else None,
        message=user_message,
        response=ai_response
    )
    db.add(chat_entry)
    await db.commit()
    await db.refresh(chat_entry)

    return ChatMessageOut(
        text=ai_response,
        sentiment=sentiment,
        suggestions=suggestions,
        vector=vector
    )

@router.get("/chat/history", response_model=list[ChatHistoryOut])
async def get_chat_history(
    db: AsyncSession = Depends(deps.get_db),
    current_user: models.User = Depends(deps.get_current_user)
):
    q = select(models.ChatHistory).where(models.ChatHistory.user_id == current_user.id).order_by(models.ChatHistory.timestamp.desc())
    res = await db.execute(q)
    return res.scalars().all()