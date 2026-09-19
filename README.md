# OfficeHours

OfficeHours is a lightweight, Vercel-ready dashboard prototype for consolidating class work, creating study plans, and generating voice-friendly summaries from a unified data pipeline.

## Process Implemented

1. User provides Canvas link + additional course sources.
2. Agentic crawler ingests campus + Canvas-like content.
3. Data is normalized into Tiger Data (represented in this starter with structured in-memory records).
4. Nemotron-style summarization endpoint generates:
   - Dashboard summary
   - Suggested schedule
   - Alexa-ready preset output
   - Completion notification payload
5. Frontend displays:
   - Assignment/test dashboard
   - Priority + time estimate views
   - Study set suggestions
   - Voice summary card and "done" notifications

## Repository Structure

- `/index.html` - Modular dashboard UI (sections are explicitly commented for easy removal/customization)
- `/styles.css` - Styling for cards, panels, and responsive layout
- `/app.js` - Frontend logic for loading tasks, requesting summaries, and rendering notifications
- `/api/nemotron.js` - Vercel serverless API scaffold for summarize / schedule / alexa / update intents
- `/vercel.json` - Vercel routing for static + API hosting

## Team Mapping (from issue)

- Website: Kenneth (implemented as modular static frontend)
- Website backend + Tiger Data: Rowan (implemented as API/data scaffold)
- Alexa skill integration path + GitHub coordination: Jared (implemented as Alexa preset API response + status notification payload)
- Agentic crawler + Nvidia Nemotron: Finn (represented through integration points in API and process docs)

## Features Included

- Centralized assignment + upcoming test dashboard
- Priority-aware ordering (projects over labs by default)
- Time estimate display per task
- Auto-generated daily study schedule
- Study set suggestions based on notes/textbook concepts
- Freeform text box to request Nemotron-backed updates
- Voice summary text suitable for website voice playback or Alexa handoff
- Completion notifications when updates are processed

## Local Run

This repo is intentionally dependency-light.

1. Install Vercel CLI if needed:
   - `npm i -g vercel`
2. From repo root:
   - `vercel dev`
3. Open the local URL shown by Vercel (usually `http://localhost:3000`).

## Deploy to Vercel

1. Connect this repository to Vercel.
2. Ensure project root is repository root.
3. Deploy (no build step required for this scaffold).

## Notes

- The backend currently uses deterministic sample data to keep the prototype runnable without external credentials.
- Replace sample `sourceData` and summarization logic in `/api/nemotron.js` with real crawler + Tiger Data + Nemotron calls when production integrations are available.
