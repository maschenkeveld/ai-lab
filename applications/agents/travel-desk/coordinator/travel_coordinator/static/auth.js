// Sign in with Keycloak: the OpenID Connect authorization code flow with PKCE, in the browser, with no libraries.
// The page redirects to Keycloak to log in, comes back with a code, trades it for tokens, and sends the access token with
// every API call. Tokens live in sessionStorage (gone when the tab closes) and are refreshed shortly before they expire.
(() => {
  const SESSION = "desk.session";
  const PKCE = "desk.pkce";
  let cfg = null;
  let session = null;

  const b64url = (bytes) => btoa(String.fromCharCode(...bytes)).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
  const random = (n) => b64url(crypto.getRandomValues(new Uint8Array(n)));
  const redirectUri = () => `${location.origin}${location.pathname}`;

  // SHA-256. crypto.subtle only exists on secure origins (https, or localhost), and this lab is served over plain http,
  // so a small fallback is included. It is checked against Node's implementation in the repo's tests.
  const K = [
    0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1, 0x923f82a4, 0xab1c5ed5,
    0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3, 0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174,
    0xe49b69c1, 0xefbe4786, 0x0fc19dc6, 0x240ca1cc, 0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da,
    0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7, 0xc6e00bf3, 0xd5a79147, 0x06ca6351, 0x14292967,
    0x27b70a85, 0x2e1b2138, 0x4d2c6dfc, 0x53380d13, 0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85,
    0xa2bfe8a1, 0xa81a664b, 0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070,
    0x19a4c116, 0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a, 0x5b9cca4f, 0x682e6ff3,
    0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208, 0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2,
  ];
  function sha256Fallback(bytes) {
    const rotr = (x, n) => (x >>> n) | (x << (32 - n));
    const h = [0x6a09e667, 0xbb67ae85, 0x3c6ef372, 0xa54ff53a, 0x510e527f, 0x9b05688c, 0x1f83d9ab, 0x5be0cd19];
    const length = bytes.length;
    const padded = new Uint8Array((((length + 9 + 63) >> 6) << 6));
    padded.set(bytes);
    padded[length] = 0x80;
    const view = new DataView(padded.buffer);
    view.setUint32(padded.length - 8, Math.floor((length * 8) / 0x100000000));
    view.setUint32(padded.length - 4, (length * 8) >>> 0);
    const w = new Uint32Array(64);
    for (let offset = 0; offset < padded.length; offset += 64) {
      for (let i = 0; i < 16; i++) w[i] = view.getUint32(offset + i * 4);
      for (let i = 16; i < 64; i++) {
        const s0 = rotr(w[i - 15], 7) ^ rotr(w[i - 15], 18) ^ (w[i - 15] >>> 3);
        const s1 = rotr(w[i - 2], 17) ^ rotr(w[i - 2], 19) ^ (w[i - 2] >>> 10);
        w[i] = (w[i - 16] + s0 + w[i - 7] + s1) >>> 0;
      }
      let [a, b, c, d, e, f, g, hh] = h;
      for (let i = 0; i < 64; i++) {
        const t1 = (hh + (rotr(e, 6) ^ rotr(e, 11) ^ rotr(e, 25)) + ((e & f) ^ (~e & g)) + K[i] + w[i]) >>> 0;
        const t2 = ((rotr(a, 2) ^ rotr(a, 13) ^ rotr(a, 22)) + ((a & b) ^ (a & c) ^ (b & c))) >>> 0;
        hh = g; g = f; f = e; e = (d + t1) >>> 0; d = c; c = b; b = a; a = (t1 + t2) >>> 0;
      }
      h[0] = (h[0] + a) >>> 0; h[1] = (h[1] + b) >>> 0; h[2] = (h[2] + c) >>> 0; h[3] = (h[3] + d) >>> 0;
      h[4] = (h[4] + e) >>> 0; h[5] = (h[5] + f) >>> 0; h[6] = (h[6] + g) >>> 0; h[7] = (h[7] + hh) >>> 0;
    }
    const out = new Uint8Array(32);
    const outView = new DataView(out.buffer);
    h.forEach((x, i) => outView.setUint32(i * 4, x));
    return out;
  }
  const sha256 = async (text) => {
    const data = new TextEncoder().encode(text);
    return crypto.subtle ? new Uint8Array(await crypto.subtle.digest("SHA-256", data)) : sha256Fallback(data);
  };

  async function login() {
    const verifier = random(48);
    const state = random(16);
    sessionStorage.setItem(PKCE, JSON.stringify({ verifier, state }));
    const params = new URLSearchParams({
      client_id: cfg.client_id, redirect_uri: redirectUri(), response_type: "code", scope: "openid",
      state, code_challenge: b64url(await sha256(verifier)), code_challenge_method: "S256",
    });
    location.assign(`${cfg.authorization_endpoint}?${params}`);
    await new Promise(() => {}); // the page is navigating away; never resolve
  }

  function keep(tokens) {
    session = { access: tokens.access_token, refresh: tokens.refresh_token, id: tokens.id_token, exp: Date.now() + tokens.expires_in * 1000 };
    sessionStorage.setItem(SESSION, JSON.stringify(session));
  }

  async function post(body) {
    const r = await fetch(cfg.token_endpoint, { method: "POST", headers: { "Content-Type": "application/x-www-form-urlencoded" }, body: new URLSearchParams(body) });
    if (!r.ok) throw new Error(`sign-in failed (${r.status})`);
    return r.json();
  }

  async function finishLogin(params) {
    const saved = JSON.parse(sessionStorage.getItem(PKCE) || "null");
    sessionStorage.removeItem(PKCE);
    if (!saved || saved.state !== params.get("state")) throw new Error("sign-in failed: the state did not match");
    keep(await post({ grant_type: "authorization_code", client_id: cfg.client_id, code: params.get("code"), redirect_uri: redirectUri(), code_verifier: saved.verifier }));
  }

  async function refresh() {
    keep(await post({ grant_type: "refresh_token", client_id: cfg.client_id, refresh_token: session.refresh }));
  }

  window.deskAuth = {
    sha256Fallback, // exported only so it can be tested
    async init(config) {
      cfg = config;
      session = JSON.parse(sessionStorage.getItem(SESSION) || "null");
      const params = new URLSearchParams(location.search);
      if (params.has("error")) throw new Error(`sign-in failed: ${params.get("error_description") || params.get("error")}`);
      if (params.has("code")) {
        await finishLogin(params);
        history.replaceState(null, "", location.pathname);
      } else if (!session) {
        await login();
      }
    },
    login,
    async token() {
      if (!session) await login();
      if (Date.now() > session.exp - 30000) {
        try { await refresh(); } catch { sessionStorage.removeItem(SESSION); await login(); }
      }
      return session.access;
    },
    signOut() {
      const idToken = session?.id;
      sessionStorage.removeItem(SESSION);
      const params = new URLSearchParams({ client_id: cfg.client_id, post_logout_redirect_uri: redirectUri() });
      if (idToken) params.set("id_token_hint", idToken);
      location.assign(`${cfg.end_session_endpoint}?${params}`);
    },
  };
})();
