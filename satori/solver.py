"""The app's brain: takes one or more images and returns the answers.

This module knows nothing about cameras or UI. The web version (FastAPI) imports
QuizSolver as-is and exposes it behind an endpoint.
"""

import base64
import json
from typing import List, Optional, Sequence, Union

import anthropic

from .models import QuizResult

MODEL = "claude-opus-5"

SYSTEM_PROMPT = """You are an assistant that reads quizzes photographed with a camera \
and solves them.

You will receive one or more photos of a quiz (paper or screen). Your job:
1. Read every visible question, in order.
2. For each multiple-choice question, pick the correct option and transcribe it \
exactly as it appears (include the option letter or number if it has one).
3. Give a brief justification (1-2 sentences) per answer.
4. State your confidence: "high", "medium" or "low". Use "low" when the image is \
hard to read or the question is ambiguous.
5. If a question is cropped, blurry or unreadable, do not make it up: report it in \
"warnings" and omit it from "answers". Also report in "warnings" any general image \
problem (glare, angle, text too small).

Answer in the same language the quiz is written in."""

_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "answers": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "question_number": {"type": "integer"},
                    "question_text": {"type": "string"},
                    "chosen_option": {"type": "string"},
                    "reasoning": {"type": "string"},
                    "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
                },
                "required": [
                    "question_number",
                    "question_text",
                    "chosen_option",
                    "reasoning",
                    "confidence",
                ],
                "additionalProperties": False,
            },
        },
        "warnings": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["answers", "warnings"],
    "additionalProperties": False,
}


class QuizSolverError(RuntimeError):
    """The image could not be solved (refused or malformed response)."""


class QuizSolver:
    def __init__(self, client: Optional[anthropic.Anthropic] = None, model: str = MODEL):
        self.client = client or anthropic.Anthropic()
        self.model = model

    def solve(
        self,
        images: Union[bytes, Sequence[bytes]],
        media_type: str = "image/jpeg",
        extra_instructions: Optional[str] = None,
    ) -> QuizResult:
        """Solve the quiz visible in the given images.

        `images` can be a single image (bytes) or several (for example, crops
        of one long form).
        """
        if isinstance(images, (bytes, bytearray)):
            images = [bytes(images)]

        content: List[dict] = [
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": media_type,
                    "data": base64.standard_b64encode(img).decode("utf-8"),
                },
            }
            for img in images
        ]
        prompt = "Read the quiz in the image(s) and answer every question."
        if extra_instructions:
            prompt += f"\n\nAdditional user instructions: {extra_instructions}"
        content.append({"type": "text", "text": prompt})

        response = self.client.beta.messages.create(
            model=self.model,
            max_tokens=16000,
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            system=SYSTEM_PROMPT,
            output_config={"format": {"type": "json_schema", "schema": _OUTPUT_SCHEMA}},
            messages=[{"role": "user", "content": content}],
        )

        if response.stop_reason == "refusal":
            detail = ""
            if getattr(response, "stop_details", None):
                detail = f" ({response.stop_details.explanation})"
            raise QuizSolverError(f"The model declined to process the image{detail}.")

        text = next((b.text for b in response.content if b.type == "text"), None)
        if text is None:
            raise QuizSolverError(
                f"The response contains no text (stop_reason={response.stop_reason})."
            )
        try:
            return QuizResult.model_validate(json.loads(text))
        except (json.JSONDecodeError, ValueError) as exc:
            raise QuizSolverError(f"Invalid JSON response: {exc}") from exc
