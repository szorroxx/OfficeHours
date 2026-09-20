"""
HTTP surface. This is what Rowan's website backend talks to.

Run it:
    MODE=mock uvicorn server:app --reload --port 8000

Then open http://localhost:8000/docs -- FastAPI generates an interactive page
where you can click "Try it out" on any endpoint and see the real response.
Use that instead of writing curl commands by hand.

Endpoints:
    GET  /health      is everything configured? which mode am I in?
    POST /prompt      the text box on the website          <- the main one
    POST /voice       Alexa (later). Fast path, speech only.
    POST /crawl       manually re-read Canvas pages
    GET  /workload    time-series data for the trend chart
    GET  /calls       every model call this process made
"""

from __future__ import annotations

import os

from fastapi import FastAPI
from fastapi.responses import RedirectResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

import cache
import config
import orchestrator
from nemotron_client import FAST_MODEL, ORCHESTRATOR_MODEL, NemotronClient

app = FastAPI(title="Office Hours orchestrator", version="0.2")

# CORS = the browser's rule that a page from one origin can't call another
# origin unless that server says it's allowed. The Vercel frontend is a
# different origin from localhost, so without this the browser silently blocks
# every request and you get a confusing "failed to fetch" with no server logs.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # fine for the hackathon; narrow it if this ever ships
    allow_methods=["*"],
    allow_headers=["*"],
)

_client: NemotronClient | None = None


@app.get("/", include_in_schema=False)
def root() -> RedirectResponse:
    return RedirectResponse(url="/docs")


def client() -> NemotronClient:
    """Created once, on the first request, so importing this file is cheap."""
    global _client
    if _client is None:
        _client = NemotronClient()
    return _client


class PromptIn(BaseModel):
    prompt: str
    context: dict | None = None      # e.g. {"student_id": "demo-student"}


class VoiceIn(BaseModel):
    utterance: str
    session_id: str | None = None


class CrawlIn(BaseModel):
    pages: list[str] | None = None   # omit for all pages


@app.get("/health")
def health() -> dict:
    """Hit this first. It tells you what's wired up without spending anything."""
    import canvas

    return {
        "ok": True,
        "mode": cache.MODE,
        "config": config.status(),
        "models": {"orchestrator": ORCHESTRATOR_MODEL, "fast": FAST_MODEL},
        "canvas": {"source": canvas.CANVAS_SOURCE, "pages": canvas.list_sources()},
        "recorded_runs": len(list(cache.RUN_DIR.glob("*.json"))),
    }


@app.post("/prompt")
def prompt(body: PromptIn) -> dict:
    """
    The main endpoint. Reasoning on, all tools available.

    Returns:
      summary  plain-text answer from Nemotron
      display  {speech, headline, cards[]}  <- Kenneth renders cards
      trace    every tool call + reasoning  <- the "it's an agent" evidence
    """
    run = orchestrator.run(body.prompt, nem=client(), channel="web",
                           context=body.context)
    return run.to_json()


@app.post("/voice")
def voice(body: VoiceIn) -> dict:
    """
    Alexa path (not wired up yet). Reasoning off, fast model, read-only tools,
    2 turns max -- an Alexa skill endpoint times out after a few seconds.

    Flat response so Jared's handler is one line:
        speech = requests.post(url, json={"utterance": u}).json()["speech"]
    """
    run = orchestrator.run(body.utterance, nem=client(), channel="voice")
    speech = run.display.get("speech") or run.summary or "I could not get your Office Hours update."
    return {
        "speech": speech,
        "card_title": run.display.get("headline", "Office Hours"),
        "elapsed_ms": run.elapsed_ms,
        "error": run.error,
    }


@app.post("/alexa")
def alexa(body: dict) -> dict:
    """Translate Alexa Custom Skill requests into the existing voice path."""
    request = body.get("request", {})
    request_type = request.get("type")

    if request_type == "LaunchRequest":
        utterance = "Give me my Office Hours update."
    elif request_type == "IntentRequest":
        intent = request.get("intent", {})
        intent_name = intent.get("name", "")
        slots = intent.get("slots", {})

        if intent_name == "AMAZON.HelpIntent":
            return {
                "version": "1.0",
                "response": {
                    "shouldEndSession": False,
                    "outputSpeech": {
                        "type": "PlainText",
                        "text": "You can ask about what is due, overdue work, your schedule, campus events, data freshness, or workload.",
                    },
                    "reprompt": {
                        "outputSpeech": {
                            "type": "PlainText",
                            "text": "What would you like to know?",
                        }
                    },
                },
            }
        if intent_name in {"AMAZON.CancelIntent", "AMAZON.StopIntent"}:
            return {
                "version": "1.0",
                "response": {
                    "shouldEndSession": True,
                    "outputSpeech": {"type": "PlainText", "text": "Goodbye."},
                },
            }

        def slot(name: str) -> str | None:
            value = slots.get(name, {}).get("value")
            return value.strip() if isinstance(value, str) and value.strip() else None

        course = slot("Course")
        days = slot("Days")
        utterance = {
            "DueIntent": "What assignments are due"
                         + (f" in {course}" if course else "")
                         + (f" within {days} days" if days else "") + "?",
            "OverdueIntent": "What assignments are overdue?",
            "ScheduleIntent": "What is on my schedule?",
            "EventsIntent": "What events are coming up"
                            + (f" within {days} days" if days else "") + "?",
            "FreshnessIntent": "Is my coursework up to date?",
            "WorkloadIntent": "How has my workload changed"
                              + (f" over the last {days} days" if days else "") + "?",
        }.get(intent_name, "Give me my Office Hours update.")
    elif request_type == "SessionEndedRequest":
        return {"version": "1.0", "response": {"shouldEndSession": True}}
    else:
        utterance = "Give me my Office Hours update."

    result = voice(VoiceIn(
        utterance=utterance,
        session_id=body.get("session", {}).get("sessionId"),
    ))
    speech = result.get("speech") or "I could not get your Office Hours update."
    return {
        "version": "1.0",
        "response": {
            "shouldEndSession": True,
            "outputSpeech": {"type": "PlainText", "text": speech},
            "card": {
                "type": "Simple",
                "title": result.get("card_title", "Office Hours"),
                "content": speech,
            },
        },
    }


@app.post("/crawl")
def crawl(body: CrawlIn) -> dict:
    """Re-read Canvas pages directly, without going through the model."""
    import tools

    return tools.refresh_from_canvas(body.pages)


@app.get("/workload")
def workload(days: int = 30) -> dict:
    """
    Time-series data for the trend chart. Straight out of the Timescale
    hypertable via time_bucket() -- this is the Tiger Data track demo.
    """
    import tools

    return tools.get_workload_history(days)


@app.get("/calls")
def calls() -> dict:
    """Every Nemotron call this process made, with token counts."""
    return {"mode": cache.MODE, "calls": client().call_log}
