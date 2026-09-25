"""Feedback Pulse — classify customer feedback with an OpenAI-compatible LLM.

Pages: Settings (LLM endpoint), Analyze (one piece of feedback), Dashboard (aggregates).
MinIO and Postgres come from environment variables; LLM settings live in Postgres.
"""

import io
import json
import os
import re
import uuid
from datetime import datetime, timezone
from urllib.parse import urlparse

import pandas as pd
import psycopg2
import requests
import streamlit as st
from minio import Minio

SENTIMENTS = ("positive", "neutral", "negative")
CATEGORIES = ("pricing", "quality", "delivery", "support", "other")

SYSTEM_PROMPT = (
    "You classify customer feedback. Return ONLY a JSON object, no prose, no code fences, "
    'with exactly these keys: {"sentiment": "positive" | "neutral" | "negative", '
    '"category": "pricing" | "quality" | "delivery" | "support" | "other", '
    '"summary": "one short sentence"}'
)


# ---------------------------------------------------------------------------
# Connections
# ---------------------------------------------------------------------------

def pg_connect():
    # DATABASE_URL is what the DKubeX chart injects; PG_* is for running outside it.
    if os.environ.get("DATABASE_URL"):
        return psycopg2.connect(os.environ["DATABASE_URL"], connect_timeout=5)
    return psycopg2.connect(
        host=os.environ.get("PG_HOST", "localhost"),
        port=int(os.environ.get("PG_PORT", "5432")),
        user=os.environ.get("PG_USER", "postgres"),
        password=os.environ.get("PG_PASSWORD", ""),
        dbname=os.environ.get("PG_DATABASE", "postgres"),
        connect_timeout=5,
    )


def minio_client():
    endpoint = os.environ.get("MINIO_ENDPOINT", "localhost:9000")
    secure = os.environ.get("MINIO_SECURE", "false").strip().lower() in ("1", "true", "yes")
    # The platform injects the endpoint as a URL (http://host:9000); the SDK wants host:port.
    if "://" in endpoint:
        url = urlparse(endpoint)
        endpoint, secure = url.netloc, url.scheme == "https"
    return Minio(
        endpoint,
        access_key=os.environ.get("MINIO_ACCESS_KEY", ""),
        secret_key=os.environ.get("MINIO_SECRET_KEY", ""),
        secure=secure,
    )


MINIO_BUCKET = os.environ.get("MINIO_BUCKET", "feedback-pulse")


def startup():
    """Create tables and check MinIO. Stops the page with an error naming what failed."""
    try:
        with pg_connect() as conn, conn.cursor() as cur:
            cur.execute(
                "CREATE TABLE IF NOT EXISTS app_settings (key text PRIMARY KEY, value text)"
            )
            cur.execute(
                """CREATE TABLE IF NOT EXISTS feedback (
                       id serial PRIMARY KEY,
                       text text,
                       sentiment text,
                       category text,
                       summary text,
                       minio_key text,
                       created_at timestamptz DEFAULT now()
                   )"""
            )
    except Exception as e:
        st.error(f"Postgres is unreachable: {e}")
        st.stop()

    try:
        minio_client().bucket_exists(MINIO_BUCKET)
    except Exception as e:
        st.error(f"MinIO is unreachable: {e}")
        st.stop()


# ---------------------------------------------------------------------------
# Settings storage
# ---------------------------------------------------------------------------

SETTING_ENV = {"base_url": "LLM_BASE_URL", "token": "LLM_TOKEN", "model": "LLM_MODEL"}


def load_settings() -> dict:
    with pg_connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT key, value FROM app_settings")
        saved = dict(cur.fetchall())
    return {
        k: saved[k] if saved.get(k) is not None else os.environ.get(env, "")
        for k, env in SETTING_ENV.items()
    }


def save_settings(values: dict) -> None:
    with pg_connect() as conn, conn.cursor() as cur:
        for k, v in values.items():
            cur.execute(
                "INSERT INTO app_settings (key, value) VALUES (%s, %s) "
                "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
                (k, v),
            )


def auth_headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"} if token else {}


def mask(token: str) -> str:
    return "••••" + token[-4:] if token else "(none)"


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------

def page_settings():
    st.header("Settings")
    saved = load_settings()

    base_url = st.text_input(
        "LLM base URL", value=saved["base_url"], placeholder="http://<host>:<port>/v1"
    ).strip().rstrip("/")
    token = st.text_input("API token (optional)", value=saved["token"], type="password")
    st.caption(f"Saved token: {mask(saved['token'])}")

    if st.button("Fetch models", disabled=not base_url):
        try:
            r = requests.get(f"{base_url}/models", headers=auth_headers(token), timeout=10)
            if r.ok:
                ids = [m["id"] for m in r.json().get("data", []) if "id" in m]
                st.session_state["model_options"] = ids
                st.success(f"Found {len(ids)} models")
            else:
                st.error(f"Fetch failed: HTTP {r.status_code} {r.text[:300]}")
        except Exception as e:
            st.error(f"Fetch failed: {e}")

    options = list(st.session_state.get("model_options", []))
    if saved["model"] and saved["model"] not in options:
        options.append(saved["model"])
    model = st.selectbox(
        "Model",
        options,
        index=options.index(saved["model"]) if saved["model"] else None,
        accept_new_options=True,
        placeholder="Pick a fetched model or type a name",
    )

    col1, col2 = st.columns(2)
    if col1.button("Save"):
        save_settings({"base_url": base_url, "token": token, "model": model or ""})
        st.success("Settings saved")

    if col2.button("Test connection", disabled=not (base_url and model)):
        try:
            r = requests.post(
                f"{base_url}/chat/completions",
                headers=auth_headers(token),
                json={
                    "model": model,
                    "messages": [{"role": "user", "content": "ping"}],
                    "max_tokens": 5,
                },
                timeout=30,
            )
            if r.ok:
                st.success(f"Connection OK ({model})")
            else:
                st.error(f"Test failed: HTTP {r.status_code} {r.text[:300]}")
        except Exception as e:
            st.error(f"Test failed: {e}")


def parse_result(raw: str) -> dict:
    """Strip code fences, parse JSON, normalize. Raises ValueError on bad JSON."""
    text = raw.strip()
    text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("response is not a JSON object")
    sentiment = str(data.get("sentiment", "")).strip().lower()
    category = str(data.get("category", "")).strip().lower()
    return {
        "sentiment": sentiment if sentiment in SENTIMENTS else "neutral",
        "category": category if category in CATEGORIES else "other",
        "summary": str(data.get("summary", "")).strip(),
    }


def page_analyze():
    st.header("Analyze")
    cfg = load_settings()
    if not cfg["base_url"] or not cfg["model"]:
        st.warning("LLM base URL or model is not set. Open **Settings** first.")
        st.stop()

    text = st.text_area("Customer feedback", height=180)
    if not st.button("Analyze", disabled=not text.strip()):
        return

    try:
        r = requests.post(
            f"{cfg['base_url']}/chat/completions",
            headers=auth_headers(cfg["token"]),
            json={
                "model": cfg["model"],
                "temperature": 0,
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": text},
                ],
            },
            timeout=60,
        )
        r.raise_for_status()
        raw = r.json()["choices"][0]["message"]["content"] or ""
    except Exception as e:
        st.error(f"LLM request failed: {e}")
        return

    try:
        result = parse_result(raw)
    except Exception as e:
        st.error(f"Could not parse the model's response as JSON: {e}")
        st.code(raw)
        return

    st.subheader("Result")
    c1, c2 = st.columns(2)
    c1.metric("Sentiment", result["sentiment"])
    c2.metric("Category", result["category"])
    st.write(result["summary"])

    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    key = f"feedback/{ts}_{uuid.uuid4().hex[:8]}.json"
    try:
        client = minio_client()
        if not client.bucket_exists(MINIO_BUCKET):
            client.make_bucket(MINIO_BUCKET)
        body = json.dumps({"text": text, **result}).encode()
        client.put_object(
            MINIO_BUCKET, key, io.BytesIO(body), len(body), content_type="application/json"
        )
    except Exception as e:
        st.error(f"Saving to MinIO failed: {e}")
        return

    try:
        with pg_connect() as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO feedback (text, sentiment, category, summary, minio_key) "
                "VALUES (%s, %s, %s, %s, %s)",
                (text, result["sentiment"], result["category"], result["summary"], key),
            )
    except Exception as e:
        st.error(f"Saving to Postgres failed: {e}")
        return

    st.success(f"Saved to MinIO ({MINIO_BUCKET}/{key}) and Postgres")


def page_dashboard():
    st.header("Dashboard")
    with pg_connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT sentiment, count(*) FROM feedback GROUP BY sentiment ORDER BY 1")
        by_sentiment = pd.DataFrame(cur.fetchall(), columns=["sentiment", "count"])
        cur.execute("SELECT category, count(*) FROM feedback GROUP BY category ORDER BY 1")
        by_category = pd.DataFrame(cur.fetchall(), columns=["category", "count"])
        cur.execute(
            "SELECT created_at, sentiment, category, summary, text, minio_key "
            "FROM feedback ORDER BY created_at DESC LIMIT 20"
        )
        recent = pd.DataFrame(
            cur.fetchall(),
            columns=["created_at", "sentiment", "category", "summary", "text", "minio_key"],
        )

    st.subheader("By sentiment")
    st.bar_chart(by_sentiment, x="sentiment", y="count")
    st.subheader("By category")
    st.bar_chart(by_category, x="category", y="count")
    st.subheader("20 most recent")
    st.dataframe(recent, hide_index=True, width="stretch")


# ---------------------------------------------------------------------------

st.set_page_config(page_title="Feedback Pulse", layout="wide")

# Identity comes from the DKubeX gateway. When deployed as a platform app, refuse to serve
# a session that arrived without it (Streamlit cannot send a 401 status, so show it instead).
if os.environ.get("DKUBEX_REQUIRE_AUTH", "").lower() == "true" and not st.context.headers.get(
    "X-Auth-Request-User"
):
    st.error("401 — Not authenticated. Open Feedback Pulse from DKubeX.")
    st.stop()

st.sidebar.title("Feedback Pulse")
startup()

PAGES = {"Settings": page_settings, "Analyze": page_analyze, "Dashboard": page_dashboard}
PAGES[st.sidebar.radio("Page", list(PAGES))]()
