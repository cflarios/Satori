"""Data models shared between the desktop app and the web API."""

from typing import List, Literal

from pydantic import BaseModel


class Answer(BaseModel):
    question_number: int
    question_text: str
    chosen_option: str
    reasoning: str
    confidence: Literal["high", "medium", "low"]


class QuizResult(BaseModel):
    answers: List[Answer]
    warnings: List[str]
