import React, { useEffect, useState } from "react";
import { createRoot } from "react-dom/client";

const API = import.meta.env.VITE_API_BASE || "/api";

const s = {
  page:      { fontFamily: "system-ui, sans-serif", maxWidth: 1100, margin: "0 auto", padding: 24, color: "#111" },
  h1:        { fontSize: 22, fontWeight: 700, marginBottom: 4 },
  h2:        { fontSize: 17, fontWeight: 600, marginBottom: 12, marginTop: 28 },
  sub:       { color: "#666", marginBottom: 24, fontSize: 14 },
  tabs:      { display: "flex", gap: 8, marginBottom: 24 },
  tab:       (active) => ({
    padding: "6px 16px", borderRadius: 6, border: "1px solid #ddd", cursor: "pointer",
    background: active ? "#111" : "#fff", color: active ? "#fff" : "#111", fontSize: 14,
  }),
  stats:     { display: "grid", gridTemplateColumns: "repeat(3, 1fr)", gap: 12, marginBottom: 24 },
  stat:      { background: "#f9f9f9", border: "1px solid #e5e5e5", borderRadius: 8, padding: 16 },
  statVal:   { fontSize: 28, fontWeight: 700 },
  statLabel: { fontSize: 12, color: "#666", marginTop: 2 },
  card:      { border: "1px solid #e5e5e5", borderRadius: 10, padding: 20, marginBottom: 16, background: "#fff" },
  cardTitle: { fontSize: 16, fontWeight: 600, marginBottom: 8 },
  meta:      { display: "flex", gap: 16, fontSize: 12, color: "#666", marginBottom: 10, flexWrap: "wrap", alignItems: "center" },
  badge:     (color) => ({
    display: "inline-block", padding: "2px 8px", borderRadius: 99, fontSize: 11, fontWeight: 500,
    background: color === "green" ? "#dcfce7" : color === "blue" ? "#dbeafe"
      : color === "red" ? "#fee2e2" : color === "purple" ? "#ede9fe" : "#f3f4f6",
    color: color === "green" ? "#166534" : color === "blue" ? "#1e40af"
      : color === "red" ? "#991b1b" : color === "purple" ? "#5b21b6" : "#374151",
  }),
  pre:       { background: "#f6f6f6", borderRadius: 8, padding: 12, fontSize: 12, overflowX: "auto", whiteSpace: "pre-wrap" },
  btn:       (variant) => ({
    padding: "5px 12px", borderRadius: 6, border: "1px solid #ddd", cursor: "pointer", fontSize: 13,
    background: variant === "primary" ? "#111" : "#fff",
    color: variant === "primary" ? "#fff" : "#111",
    marginRight: 8,
  }),
  table:     { width: "100%", borderCollapse: "collapse", fontSize: 13 },
  th:        { textAlign: "left", padding: "8px 10px", borderBottom: "2px solid #e5e5e5", color: "#444", fontWeight: 600 },
  td:        { padding: "8px 10px", borderBottom: "1px solid #f0f0f0", verticalAlign: "top" },
  infoBox:   { background: "#f8f9ff", border: "1px solid #dbeafe", borderRadius: 8, padding: "14px 18px", marginBottom: 20, fontSize: 14 },
  pipeline:  { display: "flex", alignItems: "center", gap: 0, marginBottom: 28, flexWrap: "wrap", gap: 4 },
  pipeNode:  { background: "#f3f4f6", border: "1px solid #d1d5db", borderRadius: 8, padding: "8px 14px", fontSize: 13, fontWeight: 500 },
  pipeArrow: { color: "#9ca3af", fontSize: 18, margin: "0 4px" },
  pipeLabel: { fontSize: 11, color: "#6b7280", display: "block", fontWeight: 400, marginTop: 2 },
  cfgGrid:   { display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(220px, 1fr))", gap: 10 },
  cfgItem:   { background: "#f9f9f9", border: "1px solid #e5e5e5", borderRadius: 6, padding: "10px 14px" },
  cfgKey:    { fontSize: 11, color: "#666", marginBottom: 2 },
  cfgVal:    { fontSize: 15, fontWeight: 600 },
  triggerBox:{ background: "#fafafa", border: "1px solid #efefef", borderRadius: 6, padding: "8px 12px", fontSize: 12, color: "#555", marginBottom: 12 },
  wishBox:   { background: "#fffbeb", border: "1px dashed #fbbf24", borderRadius: 10, padding: "18px 22px", marginBottom: 16 },
  wishTitle: { fontSize: 15, fontWeight: 600, marginBottom: 6, color: "#92400e" },
  wishSub:   { fontSize: 13, color: "#78350f", lineHeight: 1.6 },
  wishTag:   { display: "inline-block", background: "#fef3c7", color: "#92400e", border: "1px solid #fcd34d", borderRadius: 99, fontSize: 11, padding: "1px 8px", marginRight: 6, fontWeight: 500 },
};

// ── Recommendation type metadata ──────────────────────────────────────────────

const REC_TYPES = [
  {
    type:    "enable_semantic_cache",
    label:   "Semantic Cache",
    color:   "blue",
    plugin:  "ai-semantic-cache",
    trigger: (cfg) => `≥${cfg?.min_cluster_requests ?? 3} semantically similar prompts in lookback window (DBSCAN clustering, eps=${cfg?.dbscan_eps ?? 0.18})`,
    value:   "Serve repeated prompts directly from Redis — the LLM call is skipped entirely.",
  },
  {
    type:    "rate_limit",
    label:   "Rate Limit",
    color:   "red",
    plugin:  "ai-rate-limiting-advanced",
    trigger: (cfg) => `A consumer accounts for ≥${Math.round((cfg?.high_consumer_token_share ?? 0.4) * 100)}% of token spend on a route AND used ≥${(cfg?.min_consumer_tokens ?? 5000).toLocaleString()} tokens`,
    value:   "Protect shared routes from a single consumer crowding out others.",
  },
  {
    type:    "model_downgrade",
    label:   "Model Downgrade",
    color:   "green",
    plugin:  "ai-proxy",
    trigger: (cfg) => `A prompt cluster averages ≤${cfg?.simple_prompt_token_threshold ?? 200} prompt tokens on an expensive model (gpt-4o, claude-3-opus, nova-pro, etc.)`,
    value:   "Route a semantic cluster to a cheaper equivalent — same quality, lower cost.",
  },
  {
    type:    "consumer_model_routing",
    label:   "Consumer Routing",
    color:   "green",
    plugin:  "ai-proxy (consumer-scoped)",
    trigger: (cfg) => `A consumer sends ≥${cfg?.min_consumer_requests ?? 5} requests averaging ≤${cfg?.simple_prompt_token_threshold ?? 200} prompt tokens on an expensive model`,
    value:   "Add a consumer-scoped ai-proxy override so only that consumer is rerouted — other consumers on the same route are unaffected.",
  },
  {
    type:    "prompt_decorator",
    label:   "Prompt Decorator",
    color:   "purple",
    plugin:  "ai-prompt-decorator",
    trigger: (cfg) => `Prompts in a cluster share a common prefix of ≥${cfg?.min_common_prefix ?? 50} characters`,
    value:   "Move repeated boilerplate into a gateway-managed system message. Clients stop sending it — saving tokens on every request.",
  },
  {
    type:    "prompt_template",
    label:   "Prompt Template",
    color:   "purple",
    plugin:  "ai-prompt-template",
    trigger: (cfg) => `Prompts share both a prefix AND a suffix (fill-in-the-blank pattern, ≥${cfg?.min_template_chars ?? 30} chars combined)`,
    value:   "Enforce a prompt structure centrally in the LLM gateway. Simplifies client code and guards against prompt injection through unstructured free-text input.",
  },
  {
    type:    "prompt_compressor",
    label:   "Prompt Compressor",
    color:   "blue",
    plugin:  "ai-prompt-compressor",
    trigger: (cfg) => `Route average prompt token count ≥${cfg?.compressor_token_threshold ?? 800} over the lookback window`,
    value:   "Compress long prompts before they reach the LLM — reduces cost and latency on every request.",
  },
  {
    type:    "pii_sanitizer",
    label:   "PII Sanitizer",
    color:   "red",
    plugin:  "ai-sanitizer",
    trigger: (cfg) => `≥${Math.round((cfg?.min_pii_rate ?? 0.1) * 100)}% of prompts on a route match PII patterns (email, phone, credit card, SSN)`,
    value:   "Redact sensitive data before it reaches the LLM and before responses are logged.",
  },
];

const TYPE_META = Object.fromEntries(REC_TYPES.map((r) => [r.type, r]));

// ── Helpers ───────────────────────────────────────────────────────────────────

function useData(path, deps = []) {
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(true);
  const load = () => {
    setLoading(true);
    fetch(`${API}${path}`)
      .then((r) => r.json())
      .then((d) => { setData(d); setLoading(false); })
      .catch(() => setLoading(false));
  };
  useEffect(load, deps);
  return { data, loading, reload: load };
}

function StatCard({ label, value }) {
  return (
    <div style={s.stat}>
      <div style={s.statVal}>{value ?? "—"}</div>
      <div style={s.statLabel}>{label}</div>
    </div>
  );
}

function TriggerDetails({ rec }) {
  const cfg = rec.suggested_config;
  if (!cfg) return null;

  const items = [];
  if (cfg.avg_prompt_tokens != null) items.push(`Avg prompt tokens: ${Math.round(cfg.avg_prompt_tokens)}`);
  if (cfg.from)                       items.push(`Current model: ${cfg.from}`);
  if (cfg.to)                         items.push(`Suggested model: ${cfg.to}`);
  if (cfg.share != null)              items.push(`Token share: ${Math.round(cfg.share * 100)}%`);
  if (cfg.tokens != null)             items.push(`Tokens used: ${cfg.tokens.toLocaleString()}`);
  if (cfg.pii_types?.length)          items.push(`PII found: ${cfg.pii_types.join(", ")}`);
  if (cfg.pii_rate != null)           items.push(`Detection rate: ${Math.round(cfg.pii_rate * 100)}%`);
  if (cfg.prefix_length != null)      items.push(`Common prefix length: ${cfg.prefix_length} chars`);
  if (cfg.common_prefix)              items.push(`Prefix: "${cfg.common_prefix.slice(0, 80)}${cfg.common_prefix.length > 80 ? "…" : ""}"`);
  if (cfg.max_prompt_tokens != null)  items.push(`Peak prompt tokens: ${cfg.max_prompt_tokens}`);
  if (cfg.requests != null)           items.push(`Requests analysed: ${cfg.requests}`);
  if (cfg.template)                   items.push(`Template: "${cfg.template.slice(0, 100)}${cfg.template.length > 100 ? "…" : ""}"`);

  if (!items.length) return null;
  return (
    <div style={s.triggerBox}>
      <strong style={{ fontSize: 11, color: "#444" }}>What triggered this · </strong>
      {items.join(" · ")}
    </div>
  );
}

// ── Tabs ─────────────────────────────────────────────────────────────────────

function Overview() {
  const { data: stats } = useData("/stats");
  const { data: cfg }   = useData("/config");
  const { data: requests, loading } = useData("/requests?limit=20");
  const window = cfg?.lookback_interval ?? "1 hour";

  return (
    <>
      <div style={s.stats}>
        <StatCard label={`Requests (${window})`}       value={stats?.total_requests?.toLocaleString()} />
        <StatCard label={`Tokens (${window})`}         value={stats?.total_tokens?.toLocaleString()} />
        <StatCard label="Avg latency"                  value={stats?.avg_latency_ms ? `${stats.avg_latency_ms}ms` : null} />
        <StatCard label="Unique consumers"             value={stats?.unique_consumers} />
        <StatCard label="Unique models"                value={stats?.unique_models} />
        <StatCard label="Unique routes"                value={stats?.unique_routes} />
      </div>

      <h3 style={{ marginBottom: 12 }}>Recent requests</h3>
      {loading ? <p>Loading…</p> : (
        <div style={{ overflowX: "auto" }}>
          <table style={s.table}>
            <thead>
              <tr>
                {["Time", "Route", "Consumer", "Model", "Tokens", "Latency", "Status"].map((h) => (
                  <th key={h} style={s.th}>{h}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {(requests || []).map((r) => (
                <tr key={r.id}>
                  <td style={s.td}>{new Date(r.received_at).toLocaleTimeString()}</td>
                  <td style={s.td}>{r.route_name || "—"}</td>
                  <td style={s.td}>{r.consumer_username || "—"}</td>
                  <td style={s.td}>{r.model || "—"}</td>
                  <td style={s.td}>{r.total_tokens || 0}</td>
                  <td style={s.td}>{r.latency_ms ? `${r.latency_ms}ms` : "—"}</td>
                  <td style={s.td}>
                    <span style={s.badge(r.status_code === 200 ? "green" : "")}>
                      {r.status_code || "—"}
                    </span>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </>
  );
}

function Recommendations() {
  const { data, loading, reload } = useData("/recommendations");

  const act = async (id, action) => {
    await fetch(`${API}/recommendations/${id}/${action}`, { method: "POST" });
    reload();
  };

  if (loading) return <p>Loading…</p>;
  if (!data?.length) return (
    <p style={{ color: "#666" }}>
      No open recommendations yet. Send a few LLM requests and wait for the analyzer CronJob to run (every 5 minutes), or trigger it manually.
    </p>
  );

  return data.map((rec) => {
    const meta = TYPE_META[rec.recommendation_type];
    return (
      <div key={rec.id} style={s.card}>
        <div style={s.cardTitle}>{rec.title}</div>
        <div style={s.meta}>
          <span style={s.badge(meta?.color || "")}>
            {meta?.label || rec.recommendation_type}
          </span>
          {meta?.plugin && <span style={{ fontFamily: "monospace", fontSize: 12 }}>{meta.plugin}</span>}
          {rec.route_name && <span>Route: {rec.route_name}</span>}
          {rec.consumer_username && <span>Consumer: {rec.consumer_username}</span>}
          {rec.cluster_requests > 0 && <span>{rec.cluster_requests} requests</span>}
          {rec.cluster_tokens > 0 && <span>{rec.cluster_tokens?.toLocaleString()} tokens</span>}
          <span style={{ marginLeft: "auto", color: "#888" }}>{Math.round((rec.confidence || 0) * 100)}% confidence</span>
        </div>

        <p style={{ fontSize: 14, marginBottom: 10, lineHeight: 1.5 }}>{rec.reason}</p>

        <TriggerDetails rec={rec} />

        <h4 style={{ fontSize: 12, fontWeight: 600, marginBottom: 6, color: "#444" }}>Suggested gateway policy</h4>
        <pre style={s.pre}>{rec.yaml_config}</pre>
        <div style={{ marginTop: 12 }}>
          <button style={s.btn("primary")} onClick={() => navigator.clipboard.writeText(rec.yaml_config || "")}>Copy config</button>
          <button style={s.btn()} onClick={() => act(rec.id, "accept")}>Accept</button>
          <button style={s.btn()} onClick={() => act(rec.id, "ignore")}>Ignore</button>
        </div>
      </div>
    );
  });
}

function Clusters() {
  const { data, loading } = useData("/clusters");

  if (loading) return <p>Loading…</p>;
  if (!data?.length) return <p style={{ color: "#666" }}>No clusters detected yet. The analyzer runs every 5 minutes.</p>;

  return data.map((c) => (
    <div key={c.id} style={s.card}>
      <div style={s.cardTitle}>{c.title}</div>
      <div style={s.meta}>
        <span>{c.requests} requests</span>
        <span>{c.total_tokens?.toLocaleString()} tokens</span>
        <span>{c.avg_latency_ms}ms avg</span>
        {c.route_name && <span>{c.route_name}</span>}
        {c.model && <span>{c.model}</span>}
      </div>
      {c.example_prompt && (
        <p style={{ fontSize: 13, color: "#444", fontStyle: "italic" }}>"{c.example_prompt}"</p>
      )}
    </div>
  ));
}

function HowItWorks() {
  const { data: cfg } = useData("/config");

  return (
    <>
      <div style={s.infoBox}>
        The LLM gateway logs every LLM request as a structured event.
        The analytics pipeline picks these up, generates semantic embeddings, clusters similar prompts, and
        runs a set of heuristic checks — then surfaces actionable gateway policy recommendations.
      </div>

      <h2 style={s.h2}>Pipeline</h2>
      <div style={s.pipeline}>
        {[
          { label: "LLM gateway", sub: "structured request logs" },
          { label: "Kafka", sub: "topic: llm-usage" },
          { label: "Ingestor", sub: "always-on, embeds prompts" },
          { label: "Postgres + pgvector", sub: "stores requests + embeddings" },
          { label: "Analyzer", sub: "CronJob, every 5 min" },
          { label: "This dashboard", sub: "reads recommendations" },
        ].map((node, i, arr) => (
          <React.Fragment key={i}>
            <div style={s.pipeNode}>
              {node.label}
              <span style={s.pipeLabel}>{node.sub}</span>
            </div>
            {i < arr.length - 1 && <span style={s.pipeArrow}>→</span>}
          </React.Fragment>
        ))}
      </div>

      <h2 style={s.h2}>Recommendation types</h2>
      <div style={{ overflowX: "auto", marginBottom: 28 }}>
        <table style={s.table}>
          <thead>
            <tr>
              <th style={s.th}>Type</th>
              <th style={s.th}>Gateway policy</th>
              <th style={s.th}>Trigger condition</th>
              <th style={s.th}>Value</th>
            </tr>
          </thead>
          <tbody>
            {REC_TYPES.map((r) => (
              <tr key={r.type}>
                <td style={s.td}>
                  <span style={s.badge(r.color)}>{r.label}</span>
                </td>
                <td style={{ ...s.td, fontFamily: "monospace", fontSize: 12 }}>{r.plugin}</td>
                <td style={{ ...s.td, color: "#555" }}>{r.trigger(cfg)}</td>
                <td style={{ ...s.td, color: "#555" }}>{r.value}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <h2 style={s.h2}>Current configuration</h2>
      {!cfg ? <p>Loading…</p> : (
        <div style={s.cfgGrid}>
          {[
            ["Lookback window",        cfg.lookback_interval],
            ["Min cluster requests",   cfg.min_cluster_requests],
            ["Min cluster tokens",     cfg.min_cluster_tokens?.toLocaleString()],
            ["DBSCAN epsilon",         cfg.dbscan_eps],
            ["Simple prompt threshold", `≤${cfg.simple_prompt_token_threshold} tokens`],
            ["Consumer token share",   `≥${Math.round(cfg.high_consumer_token_share * 100)}%`],
            ["Min consumer tokens",    cfg.min_consumer_tokens?.toLocaleString()],
            ["Min consumer requests",  cfg.min_consumer_requests],
            ["Min common prefix",      `${cfg.min_common_prefix} chars`],
            ["Min template chars",     `${cfg.min_template_chars} chars`],
            ["Compressor threshold",   `≥${cfg.compressor_token_threshold} tokens`],
            ["PII detection rate",     `≥${Math.round(cfg.min_pii_rate * 100)}%`],
          ].map(([key, val]) => (
            <div key={key} style={s.cfgItem}>
              <div style={s.cfgKey}>{key}</div>
              <div style={s.cfgVal}>{val}</div>
            </div>
          ))}
        </div>
      )}

      <h2 style={{ ...s.h2, marginTop: 36 }}>Wishlist</h2>
      <p style={{ fontSize: 14, color: "#666", marginBottom: 16 }}>
        What this pipeline could become with more time.
      </p>

      <div style={s.wishBox}>
        <div style={s.wishTitle}>
          <span style={s.wishTag}>Agent</span>
          LLM-powered analyzer
        </div>
        <p style={s.wishSub}>
          Today the analyzer is a script running fixed heuristics — token thresholds, regex patterns,
          DBSCAN clusters. The recommendations it generates are only as good as the rules it was programmed with.
        </p>
        <p style={{ ...s.wishSub, marginTop: 8 }}>
          A better version would be an <strong>agentic analyzer</strong> that reads the raw traffic data,
          calls an LLM to reason about it, and writes free-form recommendations. It could understand the
          <em> intent</em> of a route from its name and traffic patterns, identify anomalies that no
          heuristic would catch, weigh cost vs. quality trade-offs in context, and generate richer
          natural-language explanations — not just template strings.
        </p>
      </div>

      <div style={s.wishBox}>
        <div style={s.wishTitle}>
          <span style={s.wishTag}>Agent</span>
          Recommendation implementation agent
        </div>
        <p style={s.wishSub}>
          Right now "Accept" just flips a status flag in the database. A human still has to copy the YAML,
          translate it into gateway configuration, and apply it through the OSS deployment workflow.
        </p>
        <p style={{ ...s.wishSub, marginTop: 8 }}>
          A better version would be an <strong>implementation agent</strong> that takes an accepted
          recommendation, updates the relevant OSS gateway or LiteLLM configuration, validates the result,
          and applies it — closing the loop from observation to running config without manual steps.
        </p>
      </div>
    </>
  );
}

// ── App ───────────────────────────────────────────────────────────────────────

const TABS = ["Overview", "Recommendations", "Clusters", "How it works"];

function App() {
  const [tab, setTab] = useState("Overview");

  return (
    <div style={s.page}>
      <h1 style={s.h1}>OSS LLM Analytics</h1>
      <p style={s.sub}>Observe LLM traffic patterns and get suggested gateway policy recommendations.</p>
      <div style={s.tabs}>
        {TABS.map((t) => (
          <button key={t} style={s.tab(tab === t)} onClick={() => setTab(t)}>{t}</button>
        ))}
      </div>
      {tab === "Overview"      && <Overview />}
      {tab === "Recommendations" && <Recommendations />}
      {tab === "Clusters"      && <Clusters />}
      {tab === "How it works"  && <HowItWorks />}
    </div>
  );
}

createRoot(document.getElementById("root")).render(<App />);
