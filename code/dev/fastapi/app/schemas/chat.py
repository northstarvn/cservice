from pydantic import BaseModel
from typing import Optional, List

class ChatMessageIn(BaseModel):
    message: str

class Sentiment(BaseModel):
    label: str
    score: float

class ChatMessageOut(BaseModel):
    text: str
    sentiment: Optional[Sentiment]
    suggestions: Optional[List[str]]
    vector: Optional[List[float]]

class ChatHistoryCreate(BaseModel):
    user_id: int
    message: str
    response: str

class ChatHistoryOut(BaseModel):
    id: int
    user_id: int
    message: str
    response: str
    timestamp: str