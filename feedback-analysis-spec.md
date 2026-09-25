Build a Streamlit app called "Feedback Pulse" as a single app.py with sidebar
navigation between three pages: Settings, Analyze, Dashboard.
Build only what is described below. Do not add extra features.

## Configuration
- MinIO and Postgres settings read from environment variables.
- LLM settings (base URL, token, model) are managed on the Settings page and stored
  in Postgres table app_settings(key text primary key, value text).
  If no saved value exists, fall back to env vars LLM_BASE_URL, LLM_TOKEN, LLM_MODEL.

## Page "Settings"
- Input: LLM base URL, e.g. http://<host>:<port>/v1 (must include /v1).
  Strip any trailing slash before saving.
- Input: API token (type="password", optional). Show the saved token masked
  (last 4 characters only). Send the Authorization header only if a token is set.
- "Fetch models" button:
  - Enabled when a base URL is filled in.
  - GET {base_url}/models, header "Authorization: Bearer <token>", timeout 10s.
  - Parse the OpenAI-style response {"data": [{"id": "..."}]} and store the model IDs
    in st.session_state["model_options"].
  - Show "Found N models" on success, or the HTTP status / error on failure.
    A failure must not block the page.
- Model field: st.selectbox with accept_new_options=True, so the user can pick a
  fetched model or type any model name.
  - Options = st.session_state["model_options"] (empty list if not fetched).
  - If a model is saved, preselect it, and add it to the options if missing.
- "Save" button writes base URL, token, and model to app_settings.
- "Test connection" button sends a short request to {base_url}/chat/completions
  with the selected model and shows success or the error.

## Page "Analyze"
- If base URL or model is missing, show a warning to open Settings, and stop.
- One text area for a single piece of customer feedback, and an "Analyze" button,
  disabled when the text is empty or whitespace.
- POST {base_url}/chat/completions (OpenAI-compatible) with the saved model,
  temperature 0, timeout 60s. Ask it to return ONLY JSON:
  {"sentiment": "positive" | "neutral" | "negative",
   "category": "pricing" | "quality" | "delivery" | "support" | "other",
   "summary": "one short sentence"}
- Strip code fences before json.loads. If parsing fails, show the raw response and
  an error. Don't crash.
- Normalize: lowercase sentiment and category. Any sentiment outside the list
  becomes "neutral"; any category outside the list becomes "other".
- Show the result.
- Save the feedback text + result as JSON to MinIO at
  feedback/<timestamp>_<short-uuid>.json. Create the bucket if it doesn't exist.
- Insert into Postgres table feedback
  (id serial, text, sentiment, category, summary, minio_key, created_at).

## Page "Dashboard"
- Bar chart: count by sentiment (SQL GROUP BY).
- Bar chart: count by category (SQL GROUP BY).
- Table: 20 most recent entries.

## Startup
- Create tables app_settings and feedback if missing.
- If Postgres or MinIO is unreachable, show a clear error naming which one failed.

## Local testing
- Create docker-compose.yml with:
  - postgres:16 (user/password/db: feedback/feedback/feedback, port 5432)
  - minio/minio (server /data --console-address ":9001", ports 9000 and 9001,
    user/password: minioadmin/minioadmin)
- Create .env.local with matching env vars (MINIO_SECURE=false).
- Start the containers, load .env.local, run the app, and verify: tables are created,
  Settings saves, "Fetch models" fills the dropdown, "Test connection" works, and one
  feedback submission lands in both MinIO and Postgres. Fix any failures and re-verify.
- docker-compose.yml and .env.local are for local testing only; app code must not
  reference them.

## Packaging
- requirements.txt: pin streamlit>=1.45 (needed for accept_new_options).
- Dockerfile for the app, exposing port 8501.
