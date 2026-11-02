"""The web app's SHA-256 fallback (used for PKCE on plain-http origins) must agree with a real implementation."""
import shutil
import subprocess
from pathlib import Path

import pytest

AUTH_JS = Path(__file__).resolve().parents[1] / "travel_coordinator" / "static" / "auth.js"

SCRIPT = r"""
global.window = {}; global.sessionStorage = { getItem() { return null; } }; global.location = {};
global.btoa = (s) => Buffer.from(s, 'binary').toString('base64');
global.crypto = require('crypto').webcrypto;
eval(require('fs').readFileSync(process.argv[1], 'utf8'));
const { createHash } = require('crypto');
const f = window.deskAuth.sha256Fallback;
const inputs = ['', 'abc', 'a'.repeat(55), 'a'.repeat(56), 'a'.repeat(63), 'a'.repeat(64), 'a'.repeat(65), 'a'.repeat(119),
  'The quick brown fox jumps over the lazy dog', 'x'.repeat(1000), 'é😀 unicode'];
let bad = 0;
for (const s of inputs) {
  const want = createHash('sha256').update(s).digest('hex');
  const got = Buffer.from(f(new TextEncoder().encode(s))).toString('hex');
  if (want !== got) { bad++; console.log('MISMATCH', s.length); }
}
console.log(bad ? 'FAIL ' + bad : 'OK');
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_the_fallback_sha256_matches_node_on_the_padding_edges():
    out = subprocess.run(["node", "-e", SCRIPT, str(AUTH_JS)], capture_output=True, text=True, timeout=30)
    assert out.stdout.strip() == "OK", out.stdout + out.stderr
