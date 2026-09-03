# Query Warden — web

The chat UI: React + TypeScript + Vite, no server-side rendering, no
router (a single conversation thread is the whole app). Talks to the
FastAPI backend (`../api`) over two SSE endpoints — see
`src/api.ts` / `src/sse.ts`.

## Quickstart

```bash
npm install
cp .env.example .env      # points at the local API — see below
npm run dev                # http://localhost:5173
```

The backend must already be running (see `../api`'s own README /
`docker compose up`) and reachable at the URL in `.env`'s
`VITE_API_URL` — `http://127.0.0.1:8001` by default for a directly-run
`uvicorn`, or `http://localhost:8000` if you're going through
`docker compose up`.

## What's here

- `src/types.ts` — hand-maintained mirror of the backend's SSE event
  payloads and `serialize_outcome`/`serialize_plan_result` shapes
  (`api/app/api/sse.py`). No shared schema codegen for a project this
  size — if the backend's shape changes, this needs a matching edit.
- `src/sse.ts` — parses the SSE wire format from a POSTed `fetch()`'s
  streamed response body by hand. Native `EventSource` only supports
  GET, and every endpoint here needs a JSON body.
- `src/components/QueryCard.tsx` — the one card component that renders
  every state a turn can be in: streaming progress, the approval gate,
  a results table + chart, or one of the taxonomy's terminal messages
  (blocked / gave up / needs clarification / zero rows diagnosed).
- `src/components/ResultChart.tsx` — validates the model's `ChartSpec`
  against the result's actual columns before rendering anything,
  falling back to the table with a stated reason if the axes don't
  exist. Y-axis values use `Intl.NumberFormat`'s compact notation
  (`140M`, not `140000000`) — recharts' default tick formatting
  overlapped into unreadable repeated zeros on this project's revenue
  -scale numbers, caught by actually rendering the component with
  realistic data rather than assumed correct from the types alone.

## The approval gate, honestly

`QueryCard` shows an Approve/Discard gate whenever the model's own
`confidence` isn't `"high"` — matching the design doc's rule. Editing
the SQL before approving is real: the edited text is re-sent to
`/api/query/approve`, which re-runs it through the AST guard exactly
like model output (`api/app/pipeline/answer.py`'s `finish(...,
override_sql=...)`) — editing is never a privileged path.

Worth knowing before you go looking for it live: this project's local
model (`qwen2.5-coder:3b`) said `"high"` in 19 of 20 real questions
during the Phase 03 checkpoint and `"low"` exactly once — paired with a
clarifying question, which skips the approval gate entirely (there's no
SQL to approve). In practice, this gate rarely fires with this specific
model; the mechanism itself is proven directly in `api/tests/test_api.py`
via a stubbed model response, not left to chance.
