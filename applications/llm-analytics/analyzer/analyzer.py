import json
import os
import re

import pandas as pd
import psycopg
import yaml
from sklearn.cluster import DBSCAN
from sentence_transformers import SentenceTransformer

POSTGRES_DSN = os.getenv("POSTGRES_DSN", "postgresql://postgres:postgres@postgres.llm-analytics.svc.cluster.local:5432/llm_analytics")
EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "sentence-transformers/all-MiniLM-L6-v2")
MIN_CLUSTER_REQUESTS = int(os.getenv("MIN_CLUSTER_REQUESTS", "3"))
MIN_CLUSTER_TOKENS = int(os.getenv("MIN_CLUSTER_TOKENS", "500"))
DBSCAN_EPS = float(os.getenv("DBSCAN_EPS", "0.18"))
SIMPLE_PROMPT_TOKEN_THRESHOLD = int(os.getenv("SIMPLE_PROMPT_TOKEN_THRESHOLD", "200"))
HIGH_CONSUMER_TOKEN_SHARE = float(os.getenv("HIGH_CONSUMER_TOKEN_SHARE", "0.4"))
MIN_CONSUMER_TOKENS = int(os.getenv("MIN_CONSUMER_TOKENS", "5000"))
MIN_COMMON_PREFIX = int(os.getenv("MIN_COMMON_PREFIX", "50"))
MIN_CONSUMER_REQUESTS = int(os.getenv("MIN_CONSUMER_REQUESTS", "5"))
LOOKBACK_INTERVAL = os.getenv("LOOKBACK_INTERVAL", "1 hour")
COMPRESSOR_TOKEN_THRESHOLD = int(os.getenv("COMPRESSOR_TOKEN_THRESHOLD", "800"))
MIN_PII_RATE = float(os.getenv("MIN_PII_RATE", "0.1"))
MIN_TEMPLATE_CHARS = int(os.getenv("MIN_TEMPLATE_CHARS", "30"))

PII_PATTERNS = {
    "email":       re.compile(r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b'),
    "phone":       re.compile(r'\b(\+\d{1,3}[\s.-]?)?\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4}\b'),
    "creditcard":  re.compile(r'\b(?:\d{4}[\s-]?){3}\d{4}\b'),
    "ssn":         re.compile(r'\b\d{3}-\d{2}-\d{4}\b'),
}

MODEL_DOWNGRADES = {
    "gpt-4o": "gpt-4o-mini",
    "gpt-4o-2024-08-06": "gpt-4o-mini",
    "gpt-4o-2024-05-13": "gpt-4o-mini",
    "gpt-4o-2024-11-20": "gpt-4o-mini",
    "gpt-4": "gpt-4o-mini",
    "gpt-4-turbo": "gpt-4o-mini",
    "gpt-4-turbo-preview": "gpt-4o-mini",
    "claude-3-5-sonnet-20241022": "claude-3-haiku-20240307",
    "claude-3-5-sonnet-20240620": "claude-3-haiku-20240307",
    "claude-3-opus-20240229": "claude-3-haiku-20240307",
    "claude-3-sonnet-20240229": "claude-3-haiku-20240307",
    "us.amazon.nova-pro-v1:0": "us.amazon.nova-lite-v1:0",
    "amazon.nova-pro-v1:0": "amazon.nova-lite-v1:0",
}

embedder = SentenceTransformer(EMBEDDING_MODEL)


def common_prefix(strings):
    if not strings:
        return ""
    prefix = strings[0]
    for s in strings[1:]:
        while not s.startswith(prefix):
            prefix = prefix[:-1]
            if not prefix:
                return ""
    return prefix


def common_suffix(strings):
    reversed_strs = [s[::-1] for s in strings]
    return common_prefix(reversed_strs)[::-1]


def mode_or_none(series):
    m = series.dropna().mode()
    return m.iloc[0] if not m.empty else None


# ── YAML generators ───────────────────────────────────────────────────────────

def make_semantic_cache_yaml(route_name):
    config = {
        "plugins": [{
            "name": "ai-semantic-cache",
            "route": route_name or "YOUR_ROUTE",
            "config": {
                "embeddings": {
                    "auth": {
                        "header_name": "Authorization",
                        "header_value": "Bearer {vault://vault/openai_key}",
                    },
                    "model": {
                        "provider": "openai",
                        "name": "text-embedding-3-small",
                    },
                },
                "vectordb": {
                    "strategy": "redis",
                    "threshold": 0.88,
                    "dimensions": 1536,
                    "distance_metric": "cosine",
                    "redis": {
                        "host": "redis.redis.svc.cluster.local",
                        "port": 6379,
                    },
                },
            },
        }]
    }
    return yaml.dump(config, default_flow_style=False, sort_keys=False)


def make_rate_limit_yaml(route_name, consumer_username):
    config = {
        "plugins": [{
            "name": "ai-rate-limiting-advanced",
            "route": route_name or "YOUR_ROUTE",
            "config": {
                "identifier": "consumer",
                "strategy": "redis",
                "sync_rate": 10,
                "tokens_count_strategy": "total_tokens",
                "redis": {
                    "host": "redis.redis.svc.cluster.local",
                    "port": 6379,
                },
                "limit": [100000],
                "window_size": [86400],
            },
        }]
    }
    return yaml.dump(config, default_flow_style=False, sort_keys=False)


def make_model_downgrade_yaml(route_name, provider, suggested_model):
    config = {
        "plugins": [{
            "name": "ai-proxy",
            "route": route_name or "YOUR_ROUTE",
            "config": {
                "model": {
                    "provider": provider or "openai",
                    "name": suggested_model,
                    "options": {
                        "max_tokens": 4096,
                    },
                },
            },
        }]
    }
    return yaml.dump(config, default_flow_style=False, sort_keys=False)


def make_consumer_model_route_yaml(route_name, consumer_username, provider, suggested_model):
    config = {
        "plugins": [{
            "name": "ai-proxy",
            "route": route_name or "YOUR_ROUTE",
            "consumer": consumer_username or "YOUR_CONSUMER",
            "config": {
                "model": {
                    "provider": provider or "openai",
                    "name": suggested_model,
                    "options": {
                        "max_tokens": 4096,
                    },
                },
            },
        }]
    }
    return yaml.dump(config, default_flow_style=False, sort_keys=False)


def make_prompt_decorator_yaml(route_name, system_prefix):
    config = {
        "plugins": [{
            "name": "ai-prompt-decorator",
            "route": route_name or "YOUR_ROUTE",
            "config": {
                "prompts": {
                    "prepend": [{
                        "role": "system",
                        "content": system_prefix,
                    }]
                },
            },
        }]
    }
    return yaml.dump(config, default_flow_style=False, sort_keys=False)


def make_prompt_compressor_yaml(route_name):
    config = {
        "plugins": [{
            "name": "ai-prompt-compressor",
            "route": route_name or "YOUR_ROUTE",
            "config": {
                "llm": {
                    "model": {
                        "provider": "openai",
                        "name": "gpt-4o-mini",
                    },
                    "auth": {
                        "header_name": "Authorization",
                        "header_value": "Bearer {vault://vault/openai_key}",
                    },
                },
            },
        }]
    }
    return yaml.dump(config, default_flow_style=False, sort_keys=False)


def make_pii_sanitizer_yaml(route_name, pii_types):
    config = {
        "plugins": [{
            "name": "ai-sanitizer",
            "route": route_name or "YOUR_ROUTE",
            "config": {
                "anonymize": pii_types,
                "host": "ai-pii.ai-pii",
                "port": 8080,
                "redact_type": "placeholder",
                "stop_on_error": True,
                "recover_redacted": False,
            },
        }]
    }
    return yaml.dump(config, default_flow_style=False, sort_keys=False)


def make_prompt_template_yaml(route_name, template):
    config = {
        "plugins": [{
            "name": "ai-prompt-template",
            "route": route_name or "YOUR_ROUTE",
            "config": {
                "allow_untemplated_requests": True,
                "templates": [{
                    "name": "detected-pattern",
                    "template": template,
                }],
            },
        }]
    }
    return yaml.dump(config, default_flow_style=False, sort_keys=False)


# ── Helpers ───────────────────────────────────────────────────────────────────

def insert_recommendation(conn, cluster_id, rec_type, title, reason, confidence,
                          route_name, consumer_username, suggested_config, yaml_config):
    conn.execute(
        """
        INSERT INTO plugin_recommendations
            (cluster_id, recommendation_type, title, reason, confidence,
             route_name, consumer_username, suggested_config, yaml_config)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (cluster_id, rec_type, title, reason, confidence,
         route_name, consumer_username, json.dumps(suggested_config), yaml_config),
    )


def insert_cluster(conn, cluster_key, title, example_prompt, requests, total_tokens,
                   avg_latency_ms, route_name, consumer_username, model):
    return conn.execute(
        """
        INSERT INTO prompt_clusters
            (cluster_key, title, example_prompt, requests, total_tokens,
             avg_latency_ms, route_name, consumer_username, model)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING id
        """,
        (cluster_key, title, example_prompt, requests, total_tokens,
         avg_latency_ms, route_name, consumer_username, model),
    ).fetchone()[0]


# ── Analysis passes ───────────────────────────────────────────────────────────

def analyze_clusters(conn, df):
    """DBSCAN clustering → semantic cache, model downgrade, and prompt decorator recommendations."""
    if len(df) < MIN_CLUSTER_REQUESTS:
        print(f"Not enough prompts ({len(df)}). Need at least {MIN_CLUSTER_REQUESTS}.")
        return 0

    embeddings = embedder.encode(df["prompt"].tolist(), normalize_embeddings=True)
    df = df.copy()
    df["cluster_id"] = DBSCAN(eps=DBSCAN_EPS, min_samples=MIN_CLUSTER_REQUESTS, metric="cosine").fit_predict(embeddings)

    clusters = df[df["cluster_id"] != -1]
    if clusters.empty:
        print("No repeated semantic clusters found.")
        return 0

    grouped = (
        clusters.groupby("cluster_id")
        .agg(
            requests=("prompt", "count"),
            total_tokens=("total_tokens", "sum"),
            avg_prompt_tokens=("prompt_tokens", "mean"),
            avg_latency_ms=("latency_ms", "mean"),
            example_prompt=("prompt", "first"),
            route_name=("route_name", mode_or_none),
            consumer_username=("consumer_username", mode_or_none),
            model=("model", mode_or_none),
            provider=("provider", mode_or_none),
        )
        .reset_index()
    )

    created = 0
    for _, row in grouped.iterrows():
        if int(row["requests"]) < MIN_CLUSTER_REQUESTS:
            continue
        if int(row["total_tokens"] or 0) < MIN_CLUSTER_TOKENS:
            continue

        cluster_rows = df[df["cluster_id"] == row["cluster_id"]]
        pg_id = insert_cluster(
            conn,
            str(row["cluster_id"]),
            f"Repeated pattern: {str(row['example_prompt'])[:80]}",
            row["example_prompt"],
            int(row["requests"]),
            int(row["total_tokens"] or 0),
            float(row["avg_latency_ms"] or 0),
            row["route_name"],
            row["consumer_username"],
            row["model"],
        )

        # Semantic cache — cluster is already repeating, worth caching
        insert_recommendation(
            conn, pg_id,
            "enable_semantic_cache",
            "Enable AI Semantic Cache",
            f"Detected {int(row['requests'])} semantically similar prompts consuming "
            f"{int(row['total_tokens'] or 0):,} tokens over the last {LOOKBACK_INTERVAL} on route "
            f"'{row['route_name']}'. Enabling semantic caching will serve repeated patterns "
            f"from Redis and skip the LLM call entirely.",
            0.85,
            row["route_name"],
            row["consumer_username"],
            {"type": "semantic_cache", "route_name": row["route_name"], "model": row["model"]},
            make_semantic_cache_yaml(row["route_name"]),
        )
        created += 1

        # Model downgrade — short prompts on an expensive model
        model = row.get("model")
        avg_pt = row.get("avg_prompt_tokens")
        if model in MODEL_DOWNGRADES and pd.notna(avg_pt) and avg_pt <= SIMPLE_PROMPT_TOKEN_THRESHOLD:
            suggested = MODEL_DOWNGRADES[model]
            insert_recommendation(
                conn, pg_id,
                "model_downgrade",
                f"Switch cluster from {model} to {suggested}",
                f"Detected {int(row['requests'])} prompts averaging {int(avg_pt)} prompt tokens — "
                f"well within the capability of {suggested}, which is significantly cheaper. "
                f"Total: {int(row['total_tokens'] or 0):,} tokens that could be served at reduced cost.",
                0.75,
                row["route_name"],
                row["consumer_username"],
                {"type": "model_downgrade", "from": model, "to": suggested, "avg_prompt_tokens": float(avg_pt)},
                make_model_downgrade_yaml(row["route_name"], row.get("provider"), suggested),
            )
            created += 1

        # Prompt decorator — prompts share a common prefix that could be a system message
        prompts = cluster_rows["prompt"].dropna().tolist()
        if len(prompts) >= 2:
            prefix = common_prefix(prompts).strip()
            if len(prefix) >= MIN_COMMON_PREFIX:
                insert_recommendation(
                    conn, pg_id,
                    "prompt_decorator",
                    f"Extract shared context into system prompt on '{row['route_name']}'",
                    f"Detected {len(prompts)} prompts sharing a common {len(prefix)}-character prefix. "
                    f"Extracting this into an ai-prompt-decorator system message means clients no longer "
                    f"need to send it — reducing token usage on every request and standardising context "
                    f"centrally in the LLM gateway.",
                    0.80,
                    row["route_name"],
                    row["consumer_username"],
                    {"type": "prompt_decorator", "common_prefix": prefix[:200], "prefix_length": len(prefix)},
                    make_prompt_decorator_yaml(row["route_name"], prefix),
                )
                created += 1

            # Prompt template — prefix AND suffix are non-trivial → fill-in-the-blank pattern
            suffix = common_suffix(prompts).strip()
            if len(prefix) + len(suffix) >= MIN_TEMPLATE_CHARS and len(suffix) >= 5:
                template = f"{prefix}{{{{input}}}}{suffix}"
                insert_recommendation(
                    conn, pg_id,
                    "prompt_template",
                    f"Turn repeating pattern into a prompt template on '{row['route_name']}'",
                    f"Detected {len(prompts)} prompts following the same fill-in-the-blank structure: "
                    f'"{prefix[:60]}[...]{suffix[:60]}". '
                    f"Encoding this as an ai-prompt-template enforces the pattern centrally, "
                    f"simplifies client code, and guards against prompt injection through unstructured input.",
                    0.75,
                    row["route_name"],
                    row["consumer_username"],
                    {"type": "prompt_template", "prefix": prefix, "suffix": suffix, "template": template},
                    make_prompt_template_yaml(row["route_name"], template),
                )
                created += 1

    return created


def analyze_prompt_compressor(conn):
    """Routes where average prompt token count is high — suggest enabling AI Prompt Compressor."""
    df = pd.read_sql(
        f"""
        SELECT route_name,
               avg(prompt_tokens)  AS avg_prompt_tokens,
               max(prompt_tokens)  AS max_prompt_tokens,
               count(*)            AS requests,
               sum(total_tokens)   AS total_tokens
        FROM llm_requests
        WHERE received_at > now() - interval '{LOOKBACK_INTERVAL}'
          AND route_name IS NOT NULL
          AND prompt_tokens > 0
        GROUP BY route_name
        HAVING avg(prompt_tokens) >= {COMPRESSOR_TOKEN_THRESHOLD}
           AND count(*) >= {MIN_CLUSTER_REQUESTS}
        """,
        conn,
    )
    if df.empty:
        return 0

    created = 0
    for _, row in df.iterrows():
        pg_id = insert_cluster(
            conn,
            f"compressor:{row['route_name']}",
            f"High prompt token count on {row['route_name']}",
            None,
            int(row["requests"]),
            int(row["total_tokens"]),
            0,
            row["route_name"],
            None,
            None,
        )
        insert_recommendation(
            conn, pg_id,
            "prompt_compressor",
            f"Enable AI Prompt Compressor on '{row['route_name']}'",
            f"Prompts on route '{row['route_name']}' average {int(row['avg_prompt_tokens'])} tokens "
            f"(peak: {int(row['max_prompt_tokens'])}). Enabling AI Prompt Compressor will reduce prompt "
            f"size before sending to the LLM, cutting cost and latency on every request.",
            0.80,
            row["route_name"],
            None,
            {"type": "prompt_compressor", "avg_prompt_tokens": float(row["avg_prompt_tokens"]),
             "max_prompt_tokens": int(row["max_prompt_tokens"])},
            make_prompt_compressor_yaml(row["route_name"]),
        )
        created += 1

    return created


def analyze_pii(conn):
    """Detect prompts containing PII — suggest enabling AI Sanitizer on that route."""
    df = pd.read_sql(
        f"""
        SELECT route_name, prompt
        FROM llm_requests
        WHERE received_at > now() - interval '{LOOKBACK_INTERVAL}'
          AND route_name IS NOT NULL
          AND prompt IS NOT NULL
        """,
        conn,
    )
    if df.empty:
        return 0

    def detect_pii_types(text):
        return [name for name, pat in PII_PATTERNS.items() if pat.search(text)]

    df["pii_types"] = df["prompt"].apply(detect_pii_types)
    df["has_pii"] = df["pii_types"].apply(bool)

    by_route = (
        df.groupby("route_name")
        .agg(
            total=("has_pii", "count"),
            pii_count=("has_pii", "sum"),
            found_types=("pii_types", lambda x: sorted(set(t for types in x for t in types))),
        )
        .reset_index()
    )
    by_route["pii_rate"] = by_route["pii_count"] / by_route["total"]
    flagged = by_route[(by_route["pii_rate"] >= MIN_PII_RATE) & (by_route["pii_count"] >= 1)]

    created = 0
    for _, row in flagged.iterrows():
        types = row["found_types"]
        pct = int(row["pii_rate"] * 100)
        pg_id = insert_cluster(
            conn,
            f"pii:{row['route_name']}",
            f"PII detected in prompts on {row['route_name']}",
            None,
            int(row["total"]),
            0,
            0,
            row["route_name"],
            None,
            None,
        )
        insert_recommendation(
            conn, pg_id,
            "pii_sanitizer",
            f"Enable AI Sanitizer on '{row['route_name']}'",
            f"{pct}% of prompts on route '{row['route_name']}' contain PII "
            f"({', '.join(types)}). Enabling the AI Sanitizer will redact sensitive data "
            f"before it reaches the LLM and before responses are logged.",
            0.90,
            row["route_name"],
            None,
            {"type": "pii_sanitizer", "pii_types": types, "pii_rate": float(row["pii_rate"])},
            make_pii_sanitizer_yaml(row["route_name"], types),
        )
        created += 1

    return created


def analyze_rate_limits(conn):
    """Flag consumers that dominate token spend on a route."""
    df = pd.read_sql(
        f"""
        SELECT route_name, consumer_username,
               sum(total_tokens) AS consumer_tokens
        FROM llm_requests
        WHERE received_at > now() - interval '{LOOKBACK_INTERVAL}'
          AND consumer_username IS NOT NULL
          AND route_name IS NOT NULL
        GROUP BY route_name, consumer_username
        """,
        conn,
    )
    if df.empty:
        return 0

    route_totals = df.groupby("route_name")["consumer_tokens"].sum().rename("route_tokens")
    df = df.join(route_totals, on="route_name")
    df["share"] = df["consumer_tokens"] / df["route_tokens"]
    flagged = df[(df["share"] >= HIGH_CONSUMER_TOKEN_SHARE) & (df["consumer_tokens"] >= MIN_CONSUMER_TOKENS)]

    created = 0
    for _, row in flagged.iterrows():
        pg_id = insert_cluster(
            conn,
            f"rl:{row['route_name']}:{row['consumer_username']}",
            f"High token usage: {row['consumer_username']} on {row['route_name']}",
            None, 0, int(row["consumer_tokens"]), 0,
            row["route_name"], row["consumer_username"], None,
        )
        pct = int(row["share"] * 100)
        insert_recommendation(
            conn, pg_id,
            "rate_limit",
            f"Rate-limit {row['consumer_username']} on {row['route_name']}",
            f"Consumer '{row['consumer_username']}' accounts for {pct}% of token spend "
            f"({int(row['consumer_tokens']):,} tokens) on route '{row['route_name']}' over the last {LOOKBACK_INTERVAL}. "
            f"Apply a token quota to prevent this consumer from crowding out others.",
            min(0.95, 0.6 + float(row["share"]) * 0.5),
            row["route_name"],
            row["consumer_username"],
            {"type": "rate_limit", "share": float(row["share"]), "tokens": int(row["consumer_tokens"])},
            make_rate_limit_yaml(row["route_name"], row["consumer_username"]),
        )
        created += 1

    return created


def analyze_consumer_routing(conn):
    """Flag consumers who habitually send simple prompts on an expensive model.

    Unlike the cluster-based model_downgrade, this looks at the consumer's overall
    behaviour across all their requests — not just a semantic cluster. The suggested
    fix is a consumer-scoped ai-proxy plugin override so only that consumer is
    rerouted; other consumers on the same route are unaffected.
    """
    df = pd.read_sql(
        f"""
        SELECT consumer_username, route_name, model, provider,
               count(*)                  AS requests,
               avg(prompt_tokens)        AS avg_prompt_tokens,
               sum(total_tokens)         AS total_tokens,
               left(prompt, 200)         AS example_prompt
        FROM llm_requests
        WHERE received_at > now() - interval '{LOOKBACK_INTERVAL}'
          AND consumer_username IS NOT NULL
          AND route_name IS NOT NULL
          AND model IS NOT NULL
          AND prompt_tokens > 0
        GROUP BY consumer_username, route_name, model, provider, left(prompt, 200)
        """,
        conn,
    )
    if df.empty:
        return 0

    # Collapse to one row per (consumer, route, model) — take the first example prompt
    agg = (
        df.groupby(["consumer_username", "route_name", "model", "provider"])
        .agg(
            requests=("requests", "sum"),
            avg_prompt_tokens=("avg_prompt_tokens", "mean"),
            total_tokens=("total_tokens", "sum"),
            example_prompt=("example_prompt", "first"),
        )
        .reset_index()
    )

    flagged = agg[
        (agg["requests"] >= MIN_CONSUMER_REQUESTS)
        & (agg["avg_prompt_tokens"] <= SIMPLE_PROMPT_TOKEN_THRESHOLD)
        & (agg["model"].isin(MODEL_DOWNGRADES))
    ]

    created = 0
    for _, row in flagged.iterrows():
        suggested = MODEL_DOWNGRADES[row["model"]]
        pg_id = insert_cluster(
            conn,
            f"cr:{row['consumer_username']}:{row['route_name']}",
            f"Simple prompts consumer: {row['consumer_username']} on {row['route_name']}",
            row["example_prompt"],
            int(row["requests"]),
            int(row["total_tokens"]),
            0,
            row["route_name"],
            row["consumer_username"],
            row["model"],
        )
        insert_recommendation(
            conn, pg_id,
            "consumer_model_routing",
            f"Route {row['consumer_username']} to {suggested} on '{row['route_name']}'",
            f"Consumer '{row['consumer_username']}' sent {int(row['requests'])} requests averaging "
            f"{int(row['avg_prompt_tokens'])} prompt tokens — consistently simple prompts that don't "
            f"need {row['model']}. A consumer-scoped ai-proxy override will automatically route "
            f"their traffic to {suggested} without affecting any other consumers on this route.",
            0.80,
            row["route_name"],
            row["consumer_username"],
            {"type": "consumer_model_routing", "from": row["model"], "to": suggested,
             "avg_prompt_tokens": float(row["avg_prompt_tokens"]), "requests": int(row["requests"])},
            make_consumer_model_route_yaml(row["route_name"], row["consumer_username"],
                                           row["provider"], suggested),
        )
        created += 1

    return created


def main():
    with psycopg.connect(POSTGRES_DSN) as conn:
        conn.execute("DELETE FROM plugin_recommendations WHERE status = 'open'")
        conn.execute("DELETE FROM prompt_clusters WHERE id NOT IN ("
                     "  SELECT cluster_id FROM plugin_recommendations"
                     "  WHERE status IN ('accepted', 'ignored') AND cluster_id IS NOT NULL"
                     ")")
        conn.commit()

        df = pd.read_sql(
            f"""
            SELECT id, route_name, consumer_username, model, provider,
                   prompt, prompt_tokens, completion_tokens, total_tokens, latency_ms
            FROM llm_requests
            WHERE received_at > now() - interval '{LOOKBACK_INTERVAL}'
              AND prompt IS NOT NULL
              AND prompt_embedding IS NOT NULL
            """,
            conn,
        )

        cluster_recs = analyze_clusters(conn, df)
        rate_recs = analyze_rate_limits(conn)
        routing_recs = analyze_consumer_routing(conn)
        compressor_recs = analyze_prompt_compressor(conn)
        pii_recs = analyze_pii(conn)
        conn.commit()

        total = cluster_recs + rate_recs + routing_recs + compressor_recs + pii_recs
        print(f"Analysis complete — {total} recommendations "
              f"({cluster_recs} clusters, {rate_recs} rate-limit, {routing_recs} consumer routing, "
              f"{compressor_recs} compressor, {pii_recs} PII).")


if __name__ == "__main__":
    main()
