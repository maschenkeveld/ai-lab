// Travel desk SPA: no build step, no dependencies. It polls the REST API under ./api/ every couple of seconds.
const api = async (path, options) => {
  const headers = { "Content-Type": "application/json" };
  if (state.authOn) headers.Authorization = `Bearer ${await deskAuth.token()}`;
  const r = await fetch(`api/${path}`, { ...options, headers });
  const body = await r.json().catch(() => ({}));
  if (r.status === 401 && state.authOn) { await deskAuth.login(); }
  if (!r.ok) throw new Error(body.detail || body.error || `HTTP ${r.status}`);
  return body;
};
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
const label = (s) => String(s).replaceAll("_", " ");
const badge = (s) => `<span class="badge ${esc(s)}">${esc(label(s))}</span>`;
const time = (iso) => new Date(iso).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
const money = (n, c = "EUR") => (n == null ? "–" : `${Number(n).toFixed(2)} ${c}`);

const state = { selected: null, trips: [], config: null, rendered: {}, comment: {}, authOn: false, me: null };
const EXAMPLES = [
  "A culture weekend from Amsterdam around 18 December, mid budget, museums please.",
  "Food and nightlife in a city I can reach for under 100 EUR, leaving around 20 December, a day or two either way.",
  "A relaxing trip for two people... actually just me, high budget, 22 December.",
];

// Who is acting. With Keycloak on, the token says (the server ignores any name we send); with it off, a typed name.
const approver = () => (state.authOn ? state.me?.username || "" : ($("approver")?.value || "").trim());
function renderWho() {
  const el = $("who");
  if (state.authOn) {
    const role = state.me.is_approver ? "approver" : "requester";
    el.innerHTML = `Signed in as <strong>${esc(state.me.username)}</strong> <span class="badge">${role}</span> <button id="signout">Sign out</button>`;
    $("signout").addEventListener("click", () => deskAuth.signOut());
  } else {
    el.innerHTML = 'Approving as <input id="approver" placeholder="your name" maxlength="40" autocomplete="off">';
    $("approver").value = localStorage.getItem("desk.approver") || "";
    $("approver").addEventListener("input", () => { localStorage.setItem("desk.approver", approver()); state.rendered.detail = null; });
  }
}

$("examples").innerHTML = EXAMPLES.map((e, i) => `<span class="chip" data-i="${i}">example ${i + 1}</span>`).join("");
$("examples").addEventListener("click", (e) => { if (e.target.dataset.i) $("request").value = EXAMPLES[e.target.dataset.i]; });

$("create").addEventListener("click", async () => {
  $("create-error").textContent = "";
  $("create").disabled = true;
  try {
    const trip = await api("trips", { method: "POST", body: JSON.stringify({ request: $("request").value, requested_by: state.authOn ? undefined : approver() || "anonymous" }) });
    $("request").value = "";
    state.selected = trip.id;
    await refresh();
  } catch (e) { $("create-error").textContent = e.message; }
  $("create").disabled = false;
});

function renderList(trips, pending) {
  const html = trips.map((t) => `
    <li data-id="${esc(t.id)}" class="${t.id === state.selected ? "active" : ""}">
      <div class="t"><strong>${esc(t.proposal?.destination?.name || "Researching…")}</strong>${badge(t.status)}</div>
      <div class="s">${esc(t.request_text)}</div></li>`).join("");
  if (state.rendered.list !== html) { $("trips").innerHTML = html || '<li class="muted">No trips yet.</li>'; state.rendered.list = html; }
  const badgeEl = $("pending-badge");
  badgeEl.classList.toggle("hidden", pending === 0);
  badgeEl.textContent = `${pending} approval${pending === 1 ? "" : "s"} waiting`;
}
$("trips").addEventListener("click", (e) => {
  const li = e.target.closest("li[data-id]");
  if (li) { state.selected = li.dataset.id; state.rendered = {}; refresh(); }
});

function approvalCard(a, trip) {
  const approvals = a.decisions.filter((d) => d.decision === "approve");
  const bar = Array.from({ length: a.required }, (_, i) => `<span class="${i < approvals.length ? "on" : ""}"></span>`).join("");
  const p = a.proposal;
  const pending = a.status === "pending";
  const mine = state.authOn && trip.requested_by === state.me.username;
  const mayDecide = state.authOn ? state.me.is_approver && !mine : !!approver();
  const why = state.authOn ? (!state.me.is_approver ? "Only approvers can decide." : mine ? "You requested this trip, so someone else has to decide." : "")
                           : "Enter your name (top right) to decide.";
  const needsName = !mayDecide;
  const decided = a.decisions.map((d) => `<div class="muted">${esc(d.approver)} → <strong>${esc(d.decision)}</strong>${d.comment ? `: “${esc(d.comment)}”` : ""}</div>`).join("");
  const saving = p.saving ? ` · saves <strong>${money(p.saving)}</strong>` : "";
  return `<div class="card approval ${a.escalated && pending ? "escalated" : ""}">
    <div class="row"><strong>${a.kind === "rebook" ? "Rebook at a lower fare" : "Approve booking"}</strong>${badge(a.status)}
      ${a.escalated && pending ? '<span class="badge failed">escalated: nobody has answered yet</span>' : ""}</div>
    <p>${esc(a.summary)}</p>
    <div class="muted">${esc(p.origin)} → ${esc(p.to_iata)} · ${esc(p.flight.date)} · <strong>${money(p.price, p.currency)}</strong>${saving}</div>
    <div class="progress" title="${approvals.length} of ${a.required} approvals">${bar}</div>
    <div class="muted">Needs ${a.required} approval${a.required === 1 ? "" : "s"} from different people · expires ${time(a.expires_at)}</div>
    ${decided}
    ${pending ? `
      <input data-comment="${esc(a.id)}" placeholder="Comment (optional)" value="${esc(state.comment[a.id] || "")}" style="margin-top:10px">
      <div class="actions">
        <button class="good" data-act="approve" data-id="${esc(a.id)}" ${needsName ? "disabled" : ""}>Approve</button>
        ${a.kind === "booking" ? `<button data-act="edit" data-id="${esc(a.id)}" ${needsName ? "disabled" : ""}>Edit…</button>` : ""}
        <button class="bad" data-act="reject" data-id="${esc(a.id)}" ${needsName ? "disabled" : ""}>Reject</button>
        ${needsName ? `<span class="muted">${esc(why)}</span>` : ""}
      </div>` : ""}
    <p class="error" data-error="${esc(a.id)}"></p></div>`;
}

function proposalCard(trip) {
  const p = trip.proposal;
  if (!p) return "";
  const alts = (p.alternatives || []).map((a) => `${esc(a.destination)} (${money(a.price)})`).join(", ");
  return `<div class="card"><h2>Proposal</h2>
    <div class="grid">
      <div class="kv"><div class="k">Destination</div><div class="v">${esc(p.destination.name)}, ${esc(p.destination.country)}</div></div>
      <div class="kv"><div class="k">Flight</div><div class="v">${esc(p.flight.flight_number)}</div></div>
      <div class="kv"><div class="k">Date</div><div class="v">${esc(p.flight.date)}</div></div>
      <div class="kv"><div class="k">Price</div><div class="v">${money(trip.price, trip.currency)}</div></div>
      <div class="kv"><div class="k">Booking</div><div class="v">${trip.booking ? `${esc(trip.booking.booking_code)} ${badge(trip.booking.status)}` : "–"}</div></div>
    </div>
    ${p.reasoning ? `<p class="muted">${esc(p.reasoning)}</p>` : ""}
    ${alts ? `<p class="muted">Alternatives: ${alts}</p>` : ""}</div>`;
}

function renderDetail(snap) {
  const { trip, approvals, audit } = snap;
  const open = !["completed", "rejected", "expired", "cancelled", "failed"].includes(trip.status);
  const canShock = state.config?.demo_mode && (!state.authOn || state.me.is_approver) && ["booked", "rebooking"].includes(trip.status);
  const html = `<div class="stack">
    <div class="card"><div class="row"><h3 style="margin:0">${esc(trip.proposal?.destination?.name || "Trip")}</h3>${badge(trip.status)}
      <span class="muted" style="margin-left:auto">${esc(trip.id)} · requested by ${esc(trip.requested_by)}</span></div>
      <p>${esc(trip.summary || "Working on it…")}</p><p class="muted">“${esc(trip.request_text)}”</p>
      <div class="row">${open ? '<button class="bad" data-trip-act="cancel">Cancel trip</button>' : ""}
      ${canShock ? '<button data-trip-act="shock" title="Demo: push the booked fare down 25%">Simulate a fare drop</button>' : ""}</div>
      <p class="error" id="trip-error"></p></div>
    ${approvals.map((a) => approvalCard(a, trip)).join("")}
    ${proposalCard(trip)}
    <div class="card"><h2>Audit log (append-only)</h2><ol class="timeline">${audit.map((e) => `
      <li><span class="ts">${time(e.ts)}</span><span class="actor">${esc(e.actor)}</span><span>${esc(label(e.type))} <span class="muted">${esc(summarise(e.detail))}</span></span></li>`).join("")}</ol></div></div>`;
  if (state.rendered.detail !== html) { $("detail").innerHTML = html; state.rendered.detail = html; }
}
const summarise = (d) => Object.entries(d || {}).filter(([k]) => !["request", "requirements"].includes(k)).map(([k, v]) => `${k}=${typeof v === "object" ? JSON.stringify(v) : v}`).join(" ");

$("detail").addEventListener("input", (e) => { if (e.target.dataset.comment) state.comment[e.target.dataset.comment] = e.target.value; });
$("detail").addEventListener("click", async (e) => {
  const b = e.target.closest("button");
  if (!b) return;
  const fail = (id, err) => { const el = document.querySelector(id); if (el) el.textContent = err.message; };
  try {
    if (b.dataset.act === "edit") return openEdit(b.dataset.id);
    if (b.dataset.act) {
      const comment = state.comment[b.dataset.id] || "";
      b.disabled = true;
      await api(`approvals/${b.dataset.id}/decision`, { method: "POST", body: JSON.stringify({ approver: state.authOn ? undefined : approver(), decision: b.dataset.act, comment }) });
      delete state.comment[b.dataset.id];
    } else if (b.dataset.tripAct === "cancel") {
      await api(`trips/${state.selected}/cancel`, { method: "POST", body: JSON.stringify({ actor: state.authOn ? undefined : approver() || "anonymous" }) });
    } else if (b.dataset.tripAct === "shock") {
      await api(`demo/fare-drop/${state.selected}`, { method: "POST", body: JSON.stringify({ pct: -25 }) });
    }
    state.rendered.detail = null;
    refresh();
  } catch (err) { b.disabled = false; fail(b.dataset.id ? `[data-error="${b.dataset.id}"]` : "#trip-error", err); }
});

let editing = null;
async function openEdit(approvalId) {
  const snap = await api(`trips/${state.selected}`);
  const a = snap.approvals.find((x) => x.id === approvalId);
  const options = [a.proposal.destination.name, ...(a.proposal.alternatives || []).map((x) => x.destination)];
  $("edit-destination").innerHTML = options.map((o) => `<option>${esc(o)}</option>`).join("");
  $("edit-date").value = a.proposal.flight.date;
  $("edit-comment").value = "";
  editing = { id: approvalId, destination: options[0], date: a.proposal.flight.date };
  $("edit-dialog").showModal();
}
$("edit-confirm").addEventListener("click", async () => {
  const edit = {};
  if ($("edit-destination").value !== editing.destination) edit.destination = $("edit-destination").value;
  if ($("edit-date").value !== editing.date) edit.date = $("edit-date").value;
  try {
    await api(`approvals/${editing.id}/decision`, { method: "POST", body: JSON.stringify({ approver: state.authOn ? undefined : approver(), decision: "edit", comment: $("edit-comment").value, edit }) });
    state.rendered.detail = null;
    refresh();
  } catch (err) { alert(err.message); }
});

function renderPolicy(config) {
  const lines = config.policy.rules.map((r) =>
    r.price_below_eur != null && r.price_from_eur == null ? `under ${r.price_below_eur} EUR: ${r.meaning}`
      : r.price_below_eur != null ? `${r.price_from_eur}–${r.price_below_eur} EUR: ${r.meaning}` : `${r.price_from_eur} EUR and up: ${r.meaning}`);
  $("policy").innerHTML = `<strong>Approval policy</strong><br>${lines.join("<br>")}<br>Unanswered approvals escalate after ${Math.round(config.policy.escalate_after_seconds / 60)} min and expire after ${Math.round(config.policy.approval_ttl_seconds / 60)} min.`;
}

async function refresh() {
  try {
    if (!state.config) {
      state.config = await api("config");
      state.authOn = state.config.auth.enabled;
      if (state.authOn) await deskAuth.init(state.config.auth);
      state.me = await api("me");
      renderWho();
      renderPolicy(state.config);
    }
    const [trips, pending] = await Promise.all([api("trips"), state.me.is_approver ? api("approvals?status=pending") : Promise.resolve([])]);
    if (!state.selected && trips.length) state.selected = trips[0].id;
    renderList(trips, pending.length);
    if (state.selected) renderDetail(await api(`trips/${state.selected}`));
  } catch (e) {
    console.warn(e);
    if (!state.me) $("detail").innerHTML = `<div class="empty error">${esc(e.message)}</div>`;  // sign-in failed: say so on the page
  }
}
refresh();
setInterval(refresh, 2000);
