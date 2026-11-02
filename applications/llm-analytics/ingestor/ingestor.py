import hashlib
import json
import os
import time

import psycopg
from confluent_kafka import Consumer
from sentence_transformers import SentenceTransformer

KAFKA_BOOTSTRAP = os.getenv("KAFKA_BOOTSTRAP", "kafka.kafka.svc.cluster.local:9092")
KAFKA_TOPIC = os.getenv("KAFKA_TOPIC", "llm-usage")
POSTGRES_DSN = os.getenv("POSTGRES_DSN", "postgresql://postgres:postgres@postgres.llm-analytics.svc.cluster.local:5432/llm_analytics")
EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "sentence-transformers/all-MiniLM-L6-v2")

embedder = SentenceTransformer(EMBEDDING_MODEL)

consumer = Consumer({
    "bootstrap.servers": KAFKA_BOOTSTRAP,
    "group.id": "llm-analytics-ingestor",
    "auto.offset.reset": "earliest",
    "enable.auto.commit": False,
})
consumer.subscribe([KAFKA_TOPIC])


def get_nested(obj, *keys, default=None):
    cur = obj
    for key in keys:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(key, default)
        if cur is default:
            return default
    return cur


def parse_json_maybe(value):
    if isinstance(value, str):
        try:
            return json.loads(value)
        except Exception:
            return value
    return value


def extract_prompt(log):
    # Gateway integrations may add ai.proxy.payload.request when payload logging is enabled.
    ai_payload_req = parse_json_maybe(get_nested(log, "ai", "proxy", "payload", "request"))
    if isinstance(ai_payload_req, dict):
        messages = ai_payload_req.get("messages")
        if isinstance(messages, list):
            parts = [m.get("content", "") for m in messages if isinstance(m, dict) and m.get("role") != "system"]
            if parts:
                return "\n".join(p for p in parts if p)

    # Fall back to request.body (available when request_buffering: true)
    body = parse_json_maybe(get_nested(log, "request", "body"))
    if isinstance(body, dict):
        messages = body.get("messages")
        if isinstance(messages, list):
            parts = [m.get("content", "") for m in messages if isinstance(m, dict) and m.get("role") != "system"]
            if parts:
                return "\n".join(p for p in parts if p)
        if isinstance(body.get("prompt"), str):
            return body["prompt"]

    return None


def extract_model(log):
    # Some gateway integrations store model info under ai.proxy.meta.
    model = get_nested(log, "ai", "proxy", "meta", "response_model")
    if model:
        return model
    model = get_nested(log, "ai", "proxy", "meta", "request_model")
    if model:
        return model
    body = parse_json_maybe(get_nested(log, "request", "body"))
    if isinstance(body, dict):
        return body.get("model")
    return None


def extract_provider(log):
    return get_nested(log, "ai", "proxy", "meta", "provider_name")


def extract_usage(log):
    usage = get_nested(log, "ai", "proxy", "usage") or {}
    prompt_tokens = usage.get("prompt_tokens") or usage.get("input_tokens") or 0
    completion_tokens = usage.get("completion_tokens") or usage.get("output_tokens") or 0
    total_tokens = usage.get("total_tokens") or (prompt_tokens + completion_tokens)
    return int(prompt_tokens), int(completion_tokens), int(total_tokens)


def vector_to_pg(v):
    return "[" + ",".join(str(float(x)) for x in v) + "]"


def process_message(conn, msg):
    log = json.loads(msg.value().decode("utf-8"))

    with conn.transaction():
        raw_id = conn.execute(
            "INSERT INTO llm_raw_logs (kafka_topic, kafka_partition, kafka_offset, raw) "
            "VALUES (%s, %s, %s, %s) RETURNING id",
            (msg.topic(), msg.partition(), msg.offset(), json.dumps(log)),
        ).fetchone()[0]

        prompt = extract_prompt(log)
        prompt_tokens, completion_tokens, total_tokens = extract_usage(log)

        prompt_hash = None
        embedding = None
        if prompt:
            prompt_hash = hashlib.sha256(prompt.encode()).hexdigest()
            emb = embedder.encode(prompt, normalize_embeddings=True)
            embedding = vector_to_pg(emb)

        conn.execute(
            """
            INSERT INTO llm_requests (
                request_id, route_name, service_name,
                consumer_id, consumer_username,
                method, path, status_code, latency_ms,
                model, provider,
                prompt, prompt_tokens, completion_tokens, total_tokens,
                prompt_hash, prompt_embedding,
                raw_log_id
            ) VALUES (
                %s, %s, %s, %s, %s,
                %s, %s, %s, %s,
                %s, %s,
                %s, %s, %s, %s,
                %s, %s::vector,
                %s
            )
            """,
            (
                get_nested(log, "request", "id"),
                get_nested(log, "route", "name"),
                get_nested(log, "service", "name"),
                get_nested(log, "consumer", "id"),
                get_nested(log, "consumer", "username"),
                get_nested(log, "request", "method"),
                get_nested(log, "request", "uri"),
                get_nested(log, "response", "status"),
                get_nested(log, "latencies", "request"),
                extract_model(log),
                extract_provider(log),
                prompt,
                prompt_tokens,
                completion_tokens,
                total_tokens,
                prompt_hash,
                embedding,
                raw_id,
            ),
        )


def main():
    print(f"Ingestor starting — bootstrap={KAFKA_BOOTSTRAP}, topic={KAFKA_TOPIC}")
    while True:
        try:
            with psycopg.connect(POSTGRES_DSN) as conn:
                print("Connected to Postgres")
                while True:
                    msg = consumer.poll(1.0)
                    if msg is None:
                        continue
                    if msg.error():
                        print("Kafka error:", msg.error())
                        continue
                    try:
                        process_message(conn, msg)
                        consumer.commit(msg)
                    except Exception as exc:
                        print("Failed to process message:", exc)
        except Exception as exc:
            print("Connection error:", exc)
            time.sleep(5)


if __name__ == "__main__":
    main()
