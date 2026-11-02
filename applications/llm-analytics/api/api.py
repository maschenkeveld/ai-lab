import os

import psycopg
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from psycopg.rows import dict_row

POSTGRES_DSN = os.getenv("POSTGRES_DSN", "postgresql://postgres:postgres@postgres.llm-analytics.svc.cluster.local:5432/llm_analytics")
LOOKBACK_INTERVAL          = os.getenv("LOOKBACK_INTERVAL", "1 hour")
MIN_CLUSTER_REQUESTS       = int(os.getenv("MIN_CLUSTER_REQUESTS", "3"))
MIN_CLUSTER_TOKENS         = int(os.getenv("MIN_CLUSTER_TOKENS", "500"))
DBSCAN_EPS                 = float(os.getenv("DBSCAN_EPS", "0.18"))
SIMPLE_PROMPT_TOKEN_THRESHOLD = int(os.getenv("SIMPLE_PROMPT_TOKEN_THRESHOLD", "200"))
HIGH_CONSUMER_TOKEN_SHARE  = float(os.getenv("HIGH_CONSUMER_TOKEN_SHARE", "0.4"))
MIN_CONSUMER_TOKENS        = int(os.getenv("MIN_CONSUMER_TOKENS", "5000"))
MIN_COMMON_PREFIX          = int(os.getenv("MIN_COMMON_PREFIX", "50"))
MIN_CONSUMER_REQUESTS      = int(os.getenv("MIN_CONSUMER_REQUESTS", "5"))
COMPRESSOR_TOKEN_THRESHOLD = int(os.getenv("COMPRESSOR_TOKEN_THRESHOLD", "800"))
MIN_PII_RATE               = float(os.getenv("MIN_PII_RATE", "0.1"))
MIN_TEMPLATE_CHARS         = int(os.getenv("MIN_TEMPLATE_CHARS", "30"))

app = FastAPI(title="LLM Analytics API")

app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


@app.get("/health")
def health():
    return {"ok": True}


@app.get("/config")
def config():
    return {
        "lookback_interval":            LOOKBACK_INTERVAL,
        "min_cluster_requests":         MIN_CLUSTER_REQUESTS,
        "min_cluster_tokens":           MIN_CLUSTER_TOKENS,
        "dbscan_eps":                   DBSCAN_EPS,
        "simple_prompt_token_threshold": SIMPLE_PROMPT_TOKEN_THRESHOLD,
        "high_consumer_token_share":    HIGH_CONSUMER_TOKEN_SHARE,
        "min_consumer_tokens":          MIN_CONSUMER_TOKENS,
        "min_common_prefix":            MIN_COMMON_PREFIX,
        "min_consumer_requests":        MIN_CONSUMER_REQUESTS,
        "compressor_token_threshold":   COMPRESSOR_TOKEN_THRESHOLD,
        "min_pii_rate":                 MIN_PII_RATE,
        "min_template_chars":           MIN_TEMPLATE_CHARS,
    }


@app.get("/stats")
def stats():
    with psycopg.connect(POSTGRES_DSN, row_factory=dict_row) as conn:
        return conn.execute(
            f"""
            SELECT
                count(*)                                    AS total_requests,
                coalesce(sum(total_tokens), 0)              AS total_tokens,
                coalesce(avg(latency_ms), 0)::int           AS avg_latency_ms,
                count(DISTINCT consumer_username)           AS unique_consumers,
                count(DISTINCT model)                       AS unique_models,
                count(DISTINCT route_name)                  AS unique_routes
            FROM llm_requests
            WHERE received_at > now() - interval '{LOOKBACK_INTERVAL}'
            """
        ).fetchone()


@app.get("/requests")
def requests_list(limit: int = 50):
    with psycopg.connect(POSTGRES_DSN, row_factory=dict_row) as conn:
        return conn.execute(
            """
            SELECT id, received_at, route_name, service_name, consumer_username,
                   model, provider, prompt_tokens, completion_tokens, total_tokens,
                   latency_ms, status_code,
                   left(prompt, 200) AS prompt_preview
            FROM llm_requests
            ORDER BY received_at DESC
            LIMIT %s
            """,
            (limit,),
        ).fetchall()


@app.get("/clusters")
def clusters():
    with psycopg.connect(POSTGRES_DSN, row_factory=dict_row) as conn:
        return conn.execute(
            """
            SELECT id, created_at, title, left(example_prompt, 200) AS example_prompt,
                   requests, total_tokens, avg_latency_ms::int,
                   route_name, consumer_username, model
            FROM prompt_clusters
            ORDER BY total_tokens DESC
            LIMIT 50
            """
        ).fetchall()


@app.get("/recommendations")
def recommendations():
    with psycopg.connect(POSTGRES_DSN, row_factory=dict_row) as conn:
        return conn.execute(
            """
            SELECT r.id, r.created_at, r.recommendation_type, r.title, r.reason,
                   r.confidence, r.route_name, r.consumer_username,
                   r.yaml_config, r.status,
                   c.requests AS cluster_requests,
                   c.total_tokens AS cluster_tokens
            FROM plugin_recommendations r
            LEFT JOIN prompt_clusters c ON c.id = r.cluster_id
            WHERE r.status = 'open'
            ORDER BY r.created_at DESC
            LIMIT 50
            """
        ).fetchall()


@app.post("/recommendations/{rec_id}/accept")
def accept(rec_id: int):
    with psycopg.connect(POSTGRES_DSN) as conn:
        conn.execute("UPDATE plugin_recommendations SET status = 'accepted' WHERE id = %s", (rec_id,))
        conn.commit()
    return {"ok": True}


@app.post("/recommendations/{rec_id}/ignore")
def ignore(rec_id: int):
    with psycopg.connect(POSTGRES_DSN) as conn:
        conn.execute("UPDATE plugin_recommendations SET status = 'ignored' WHERE id = %s", (rec_id,))
        conn.commit()
    return {"ok": True}
