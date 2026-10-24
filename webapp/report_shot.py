"""A screenshot of the portal showing one run, for the flow report's appendix. Chrome on this host hangs on http:// pages, so the
portal's own page (static/index.html) is loaded from a file:// copy whose fetch() answers from the portal's API responses, captured
in-process: the page renders exactly as it does live, with the run opened, its charts showing the whole run, in the light theme."""
import json
import shutil
import subprocess
import tempfile
from pathlib import Path

STATIC = Path(__file__).resolve().parent / "static" / "index.html"

STUB = """<script>
try { localStorage.setItem('portal-theme', 'light'); } catch (e) { }
const STUB = %(stub)s, RUN_ID = %(rid)s, RUN_DATA = %(run)s;
window.fetch = async (p, o) => {
  let body = {};
  if (p.startsWith('/api/runs/' + RUN_ID)) body = /since=0(&|$)/.test(p) ? RUN_DATA : {...RUN_DATA, log: [], log_offset: RUN_DATA.log.length};
  else { const k = Object.keys(STUB).sort((a, b) => b.length - a.length).find(k => p.startsWith(k)); if (k) body = STUB[k]; }
  return new Response(JSON.stringify(body), {status: 200, headers: {'content-type': 'application/json'}});
};
</script>"""

OPEN = """<script>
setTimeout(() => { openRun(%(rid)s); }, 300);
setTimeout(() => {   // the Flows controls as this run was started: mode toggles, sweep, duration, direction, algorithms, bit rate
  const o = RUN_DATA.options || {}, m = RUN_DATA.mode;
  KIND = m === 'live' ? 'live' : 'timed'; SEQ = m === 'solo' || !!o.sequential; SWEEP = !!o.rtt_sweep; setMode();
  const dv = o.rtt_sweep ? o.step_s : m === 'live' ? (o.sequential ? o.slot_s : o.max_s) : o.duration, sel = document.getElementById('dur');
  if (dv) { if (![...sel.options].some(x => +x.value === dv)) sel.add(new Option(dv + ' s', dv)); sel.value = dv; }
  if (o.direction) document.getElementById('dir').value = o.direction;
  if (m === 'compete') document.getElementById('stag').value = String(o.stagger || 0);
  ALGOS.forEach(a => { const c = document.getElementById('al_' + a); if (c) c.checked = (o.algos || ALGOS).includes(a); });
  const br = o.bitrate_mbit || 0; document.getElementById('br').value = nearest(BR, br); showBr(br);
}, 900);
setTimeout(() => { const w = document.getElementById('win'); w.value = w.max; w.dispatchEvent(new Event('input')); }, 1200);   // the whole run
setTimeout(() => { document.documentElement.dataset.theme = 'light'; window.scrollTo(0, 0); }, 1500);
</script>"""


def screenshot(stubs, run, out_png, width=1300, height=1560):
    """stubs: {"/api/lab": {...}, "/api/state": {...}, ...}; run: the run as /api/runs/<id> returns it. Writes out_png; returns its path."""
    html = STATIC.read_text()
    html = html.replace("<head>", "<head>" + STUB % {"stub": json.dumps(stubs), "rid": json.dumps(run["id"]), "run": json.dumps(run)}, 1)
    html = html.replace("</body>", OPEN % {"rid": json.dumps(run["id"])} + "</body>", 1)
    tmp = Path(tempfile.mkdtemp(prefix="cc-shot-"))
    try:
        (tmp / "portal.html").write_text(html)
        r = subprocess.run(["google-chrome", "--headless=new", "--disable-gpu", "--no-sandbox", "--hide-scrollbars", f"--user-data-dir={tmp / 'profile'}",
                            f"--window-size={width},{height}", "--virtual-time-budget=6000", f"--screenshot={tmp / 'shot.png'}", f"file://{tmp / 'portal.html'}"],
                           capture_output=True, timeout=90)
        if not (tmp / "shot.png").exists(): raise RuntimeError(f"chrome did not write the screenshot (rc={r.returncode}): {r.stderr.decode()[-300:]}")
        shutil.move(str(tmp / "shot.png"), out_png)
        return out_png
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
