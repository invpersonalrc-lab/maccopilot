import io
import json
import os
import threading
import time
from collections import deque
from flask import Flask, render_template_string, request, jsonify, send_file
from PIL import ImageGrab
import pyautogui

app = Flask(__name__)
pyautogui.FAILSAFE = True
pending = {}
_audit_log = deque(maxlen=500)
_state_lock = threading.Lock()

# This prototype exposes only individually approved, narrowly allowlisted actions.
# GREEN labels a proposal as lower-risk; it never causes autonomous execution.
# YELLOW/RED/unknown operation classes are not exposed as executable actions.
ALLOWED_ACTIONS = frozenset({"click", "type", "hotkey"})
ALLOWED_HOTKEYS = frozenset({"command+l", "command+tab", "command+w", "esc", "enter", "tab"})
MAX_PENDING = 1
MAX_ACTIONS_PER_SESSION = 20
SESSION_WINDOW_SECONDS = 3600
PROPOSAL_TTL_SECONDS = 60
_action_times = deque()

HTML = '''<!doctype html><html><head><meta charset="utf-8"><title>Mac Copilot</title><style>body{font:16px system-ui;max-width:900px;margin:35px auto;padding:0 18px;background:#f7f7f7;color:#222}button,input,select{font:inherit;padding:10px;margin:5px}button{cursor:pointer}img{max-width:100%;border:1px solid #aaa;border-radius:8px}.panel{background:white;border-radius:12px;padding:18px;margin:15px 0}#status{white-space:pre-wrap}</style></head><body><h1>Mac Copilot · Approval Mode</h1><p>Local-only prototype. Nothing clicks or types until you approve. Move your mouse to the upper-left corner for PyAutoGUI emergency stop.</p><div class="panel"><button onclick="capture()">Capture screen</button><img id="screen" alt="Screen capture appears here"></div><div class="panel"><h3>Propose one action</h3><select id="kind"><option value="click">Click at coordinates</option><option value="type">Type text</option><option value="hotkey">Press shortcut</option></select><input id="x" type="number" placeholder="X" value="300"><input id="y" type="number" placeholder="Y" value="300"><input id="value" placeholder="Text or shortcut (e.g. command+l)" style="width:300px"><button id="propose-button">Propose</button><p id="proposal"></p><button id="approve" disabled>Approve one action</button><button id="reject">Reject / Stop</button><p id="status"></p></div><script>
const els = {
  screen: document.getElementById('screen'), kind: document.getElementById('kind'),
  x: document.getElementById('x'), y: document.getElementById('y'), value: document.getElementById('value'),
  proposeButton: document.getElementById('propose-button'), proposal: document.getElementById('proposal'),
  approve: document.getElementById('approve'), reject: document.getElementById('reject'), status: document.getElementById('status')
};
let token = null;
async function capture(){ els.screen.src='/capture?ts='+Date.now(); }
async function propose(){
  els.approve.disabled=true; token=null;
  try {
    const r=await fetch('/propose',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({kind:els.kind.value,x:Number(els.x.value),y:Number(els.y.value),value:els.value.value})});
    const j=await r.json();
    if(!r.ok || j.error){els.status.textContent=j.error||'Proposal failed';return;}
    token=j.token; els.proposal.textContent='Pending ('+j.permission_level+'): '+j.description;
    els.approve.disabled=false; els.status.textContent='Awaiting your approval';
  } catch(e) { els.status.textContent='Proposal failed: '+e.message; }
}
async function approve(){
  if(!token)return;
  const approvedToken=token; els.approve.disabled=true;
  try {
    const r=await fetch('/approve',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({token:approvedToken})});
    const j=await r.json(); els.status.textContent=j.message||j.error||'Action failed';
    if(token===approvedToken){token=null;els.proposal.textContent='';}
  } catch(e) { els.status.textContent='Approval request failed: '+e.message; }
}
async function reject(){
  token=null; els.approve.disabled=true; els.proposal.textContent='';
  await fetch('/reject',{method:'POST'}); els.status.textContent='Pending action canceled';
}
els.proposeButton.addEventListener('click',propose); els.approve.addEventListener('click',approve); els.reject.addEventListener('click',reject);
</script></body></html>'''


def _audit(event, **details):
    # Never record typed contents; retain only event metadata and action kind.
    with _state_lock:
        _audit_log.append({"time": time.time(), "event": event, **details})


def _proposal(data):
    if not isinstance(data, dict):
        return None, "Invalid proposal"
    kind = data.get("kind")
    if kind not in ALLOWED_ACTIONS:
        return None, "Unknown or unsupported action denied"
    value = data.get("value", "")
    if not isinstance(value, str):
        return None, "Action value must be text"
    if kind == "type":
        if not value or len(value) > 250:
            return None, "Text must be 1-250 characters"
        # Typing is inherently context-sensitive. Only allow simple, non-sensitive
        # literal text; never accept credentials or operational commands.
        lower = value.lower()
        forbidden = ("password", "passwd", "secret", "token", "credential", "api_key", "apikey", "private key", "delete ", "rm -", "sudo ", "curl ", "http://", "https://")
        if any(term in lower for term in forbidden) or "\n" in value or "\r" in value:
            return None, "Potentially sensitive or operational text is not allowed"
    if kind == "hotkey" and value.lower() not in ALLOWED_HOTKEYS:
        return None, "Shortcut not allowlisted"
    try:
        x, y = int(data.get("x", 0)), int(data.get("y", 0))
    except (TypeError, ValueError, OverflowError):
        return None, "Coordinates must be integers"
    if kind == "click":
        w, h = pyautogui.size()
        if not (0 <= x < w and 0 <= y < h):
            return None, "Coordinates outside display"
    return (kind, x, y, value), None


@app.get('/')
def home():
    return render_template_string(HTML)


@app.get('/capture')
def capture():
    im = ImageGrab.grab()
    out = io.BytesIO()
    im.thumbnail((1440, 900))
    im.save(out, format='PNG')
    out.seek(0)
    return send_file(out, mimetype='image/png')


@app.post('/propose')
def propose():
    data = request.get_json(silent=True)
    action, error = _proposal(data)
    if error:
        _audit("proposal_denied", kind=data.get("kind") if isinstance(data, dict) else None)
        return jsonify(error=error), 400
    kind, x, y, value = action
    token = os.urandom(16).hex()
    desc = {"click": f"Click at ({x}, {y})", "type": f"Type: {value!r}", "hotkey": f"Press: {value}"}[kind]
    with _state_lock:
        if len(pending) >= MAX_PENDING:
            return jsonify(error="A proposal is already pending; reject it first"), 429
        pending[token] = (time.monotonic(), kind, x, y, value)
    _audit("proposed", kind=kind, permission_level="GREEN", token=token)
    return jsonify(token=token, description=desc, permission_level="GREEN")


@app.post('/approve')
def approve():
    data = request.get_json(silent=True)
    token = data.get("token") if isinstance(data, dict) else None
    with _state_lock:
        entry = pending.pop(token, None) if isinstance(token, str) else None
        now = time.monotonic()
        while _action_times and now - _action_times[0] >= SESSION_WINDOW_SECONDS:
            _action_times.popleft()
        if len(_action_times) >= MAX_ACTIONS_PER_SESSION:
            if entry:
                pending[token] = entry
            _audit_log.append({"time": time.time(), "event": "rate_limited"})
            return jsonify(error="Action limit reached; wait before approving more actions"), 429
    if not entry:
        _audit("approval_denied", reason="no_pending_action")
        return jsonify(error="No pending action"), 400
    created, kind, x, y, value = entry
    if time.monotonic() - created > PROPOSAL_TTL_SECONDS:
        _audit("approval_denied", kind=kind, reason="expired")
        return jsonify(error="Approval expired"), 400
    if kind not in ALLOWED_ACTIONS:
        _audit("approval_denied", kind="unknown", reason="unknown_action")
        return jsonify(error="Unknown action denied"), 400
    # Revalidate at the execution boundary; no model- or client-supplied permission
    # level is trusted. The only execution path is this explicit human approval.
    _action_times.append(time.monotonic())
    _audit("approved", kind=kind, token=token)
    try:
        if kind == 'click':
            pyautogui.click(x, y)
        elif kind == 'type':
            pyautogui.write(value, interval=.04)
        elif value.lower() in ('esc', 'enter', 'tab'):
            pyautogui.press(value.lower())
        else:
            pyautogui.hotkey(*value.lower().split('+'))
        _audit("executed", kind=kind, token=token)
        return jsonify(message='One approved action executed')
    except Exception as e:
        _audit("execution_failed", kind=kind, error=type(e).__name__)
        return jsonify(error=f'Action failed: {type(e).__name__}: {e}'), 500


@app.post('/reject')
def reject():
    with _state_lock:
        count = len(pending)
        pending.clear()
    _audit("rejected", count=count)
    return jsonify(message='Canceled')


@app.get('/audit')
def audit():
    # Local audit metadata only; no typed contents or screenshots are retained.
    with _state_lock:
        events = list(_audit_log)
    return jsonify(events=events)


if __name__ == '__main__':
    app.run(host='127.0.0.1', port=8765, debug=False)
