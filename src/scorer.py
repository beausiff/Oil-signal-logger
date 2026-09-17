"""Claude scoring call plus strict JSON validation.

One retry on invalid output. A second failure returns None and the caller logs
an `error` row and skips trade logic for that hour.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Sequence

import anthropic

from . import config

PROMPT_PATH = Path(__file__).resolve().parent.parent / "prompts" / "scorer_system.md"

VALID_CONFIDENCE = {"low", "medium", "high"}
VALID_DIRECTION = {"escalation", "de_escalation", "neutral"}

_JSON_BLOCK = re.compile(r"\{.*\}", re.DOTALL)


@dataclass
class Score:
    score: int
    confidence: str
    direction: str
    key_headlines: List[str] = field(default_factory=list)
    new_information: bool = False
    reasoning: str = ""


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    attempts: int = 0
    error: str = ""

    @property
    def cost_usd(self) -> float:
        return round(
            self.input_tokens / 1_000_000 * config.COST_PER_MTOK_INPUT
            + self.output_tokens / 1_000_000 * config.COST_PER_MTOK_OUTPUT,
            6,
        )

    def as_note(self) -> str:
        return "tokens_in=%d tokens_out=%d cost_usd=%.6f attempts=%d" % (
            self.input_tokens,
            self.output_tokens,
            self.cost_usd,
            self.attempts,
        )


def load_system_prompt() -> str:
    return PROMPT_PATH.read_text(encoding="utf-8")


def build_user_message(headlines: Sequence[dict], recent_signals: Sequence[dict]) -> str:
    """Headlines and prior scores are data. The system prompt says so too."""
    lines = ["<new_headlines>"]
    if not headlines:
        lines.append("(none in the last hour)")
    for item in headlines:
        lines.append(
            "- [%s | %s] %s"
            % (item.get("published_utc", ""), item.get("source", ""), item.get("title", ""))
        )
    lines.append("</new_headlines>")

    lines.append("<last_24_scores>")
    if not recent_signals:
        lines.append("(no prior scores)")
    for row in recent_signals:
        lines.append(
            "- %s score=%s confidence=%s direction=%s reasoning=%s"
            % (
                row.get("run_utc", ""),
                row.get("score", ""),
                row.get("confidence", ""),
                row.get("direction", ""),
                row.get("reasoning", ""),
            )
        )
    lines.append("</last_24_scores>")
    lines.append("")
    lines.append("Return the JSON object only.")
    return "\n".join(lines)


def parse_and_validate(text: str) -> Score:
    """Raise ValueError on anything that is not the exact contract."""
    match = _JSON_BLOCK.search(text or "")
    if not match:
        raise ValueError("no JSON object in response")

    payload = json.loads(match.group(0))
    if not isinstance(payload, dict):
        raise ValueError("response is not a JSON object")

    raw_score = payload.get("score")
    if isinstance(raw_score, bool) or not isinstance(raw_score, int):
        raise ValueError("score must be an integer")
    if not -10 <= raw_score <= 10:
        raise ValueError("score out of range")

    confidence = payload.get("confidence")
    if confidence not in VALID_CONFIDENCE:
        raise ValueError("bad confidence: %r" % (confidence,))

    direction = payload.get("direction")
    if direction not in VALID_DIRECTION:
        raise ValueError("bad direction: %r" % (direction,))

    key_headlines = payload.get("key_headlines", [])
    if not isinstance(key_headlines, list):
        raise ValueError("key_headlines must be a list")
    key_headlines = [str(x) for x in key_headlines][:3]

    new_information = payload.get("new_information")
    if not isinstance(new_information, bool):
        raise ValueError("new_information must be a boolean")

    reasoning = str(payload.get("reasoning", "")).strip()

    return Score(
        score=raw_score,
        confidence=confidence,
        direction=direction,
        key_headlines=key_headlines,
        new_information=new_information,
        reasoning=reasoning,
    )


def _client() -> anthropic.Anthropic:
    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY is not set")
    return anthropic.Anthropic(api_key=api_key)


def score_news(
    headlines: Sequence[dict],
    recent_signals: Sequence[dict],
    client: Optional[anthropic.Anthropic] = None,
):
    """Return (Score|None, Usage).

    None means we could not get a valid score this hour, for any reason: a
    missing key, an API failure, or two rounds of invalid JSON. The caller logs
    an `error` row and skips the trade logic. An hourly job never dies because
    one dependency was unavailable for one hour.
    """
    usage = Usage()

    try:
        client = client or _client()
    except RuntimeError as exc:
        usage.error = "scorer_no_key"
        print("scorer unavailable: %s" % exc)
        return None, usage

    system_prompt = load_system_prompt()
    user_message = build_user_message(headlines, recent_signals)

    messages = [{"role": "user", "content": user_message}]
    last_error = ""

    for attempt in (1, 2):
        usage.attempts = attempt

        try:
            response = client.messages.create(
                model=config.ANTHROPIC_MODEL,
                max_tokens=config.ANTHROPIC_MAX_TOKENS,
                output_config={"effort": config.ANTHROPIC_EFFORT},
                system=system_prompt,
                messages=messages,
            )
        except Exception as exc:  # noqa: BLE001 - auth, rate limit, overload, network
            last_error = "%s: %s" % (type(exc).__name__, exc)
            # Carry the reason into the sheet: the exception class alone does
            # not say whether it was the key, the model or the request shape.
            usage.error = "scorer_api_error:%s:%s" % (
                type(exc).__name__,
                str(exc).replace("\n", " ")[:160],
            )
            print("scorer call failed (attempt %d): %s" % (attempt, last_error))
            if attempt == 2:
                return None, usage
            continue

        usage.input_tokens += getattr(response.usage, "input_tokens", 0) or 0
        usage.output_tokens += getattr(response.usage, "output_tokens", 0) or 0

        text = "".join(
            block.text for block in response.content if getattr(block, "type", "") == "text"
        )
        try:
            return parse_and_validate(text), usage
        except (ValueError, json.JSONDecodeError) as exc:
            last_error = str(exc)
            usage.error = "scorer_invalid_json"
            if attempt == 2:
                break
            messages = [
                {"role": "user", "content": user_message},
                {"role": "assistant", "content": text or "(empty)"},
                {
                    "role": "user",
                    "content": (
                        "That was invalid (%s). Return the JSON object only, "
                        "no prose, no code fences." % last_error
                    ),
                },
            ]

    print("scorer failed twice: %s" % last_error)
    return None, usage
