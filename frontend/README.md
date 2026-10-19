# Ledger frontend

A dependency-free browser interface for the 10-Q research copilot API.

## Run locally

1. Start the API from `backend/` with `uvicorn api:app --reload --port 8000`.
2. In another terminal, serve this directory on port 5173, for example with `python -m http.server 5173` from `frontend/`.
3. Open `http://localhost:5173`.

The UI defaults to `http://localhost:8000` for the API. To use another API origin, add `?api=http://your-host:port` to the frontend URL and allow that origin in the API CORS configuration.

The frontend uses the existing companies, filings, ask, sessions, table, PDF, and health endpoints. No frontend package installation is needed.
