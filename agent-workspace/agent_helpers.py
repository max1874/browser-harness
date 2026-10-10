"""Agent-editable browser helpers.

Add task-specific browser primitives here. Core helpers from browser_harness.helpers
load this file when BH_AGENT_WORKSPACE points at this directory, or when this
repo's default agent-workspace exists.
"""

import time as _time

from browser_harness.helpers import cdp, click_at_xy


def ax_nodes():
    return cdp("Accessibility.getFullAXTree")["nodes"]


def ax_role_name(node):
    return (
        node.get("role", {}).get("value"),
        ((node.get("name", {}) or {}).get("value") or "").strip(),
    )


def ax_find(name, role=None, exact=False, nth=0):
    """First (or nth) accessibility node whose name matches."""
    hits = []
    for node in ax_nodes():
        r, nm = ax_role_name(node)
        if role and r != role:
            continue
        if not nm:
            continue
        if (nm == name) if exact else (name.lower() in nm.lower() or name == ""):
            hits.append(node)
    return hits[nth] if len(hits) > nth else None


def ax_center(node):
    quad = cdp("DOM.getBoxModel", backendNodeId=node["backendDOMNodeId"])
    q = quad["model"]["content"]
    return sum(q[0::2]) / 4, sum(q[1::2]) / 4


def ax_click(name, role=None, exact=False, nth=0, settle=1.0):
    node = ax_find(name, role=role, exact=exact, nth=nth)
    if node is None:
        raise LookupError(f"no accessibility node named {name!r} (role={role})")
    x, y = ax_center(node)
    click_at_xy(x, y)
    if settle:
        _time.sleep(settle)
    return x, y


def ax_dump(roles=("button", "link", "menuitem", "menuitemradio", "option",
                   "combobox", "tab", "heading", "textbox"), limit=120):
    out = []
    for node in ax_nodes():
        r, nm = ax_role_name(node)
        if r in roles and nm:
            out.append(f"{r}: {nm[:90]}")
    return out[:limit]


# --- nex-studio chat acceptance helpers -------------------------------------

NEX_NEW_CHAT = "https://studio-stg.nex.ad/o/spacex/b/spacex/chat/new"


def nex_chip_titles():
    from browser_harness.helpers import js
    import json as _json
    return _json.loads(js(
        """JSON.stringify([...document.querySelectorAll('button[aria-label="Remove attachment"]')]
           .map(b => b.closest('div').parentElement.getAttribute('title')))"""))


def nex_submit_disabled():
    from browser_harness.helpers import js
    return js("""(()=>{const b=[...document.querySelectorAll('button')]
        .find(b=>(b.getAttribute('aria-label')||'')==='Submit');
        return b ? String(b.disabled) : 'missing'})()""") == "true"


def nex_open_chat(files=(), settle=2.5, ready_timeout=90):
    """New CF chat, optionally with attachments, waited to ready."""
    from browser_harness.helpers import goto_url, wait_for_load, upload_file, cdp
    goto_url(NEX_NEW_CHAT)
    wait_for_load()
    _time.sleep(settle)
    cdp("Network.enable")
    if not files:
        return []
    upload_file("input[type=file]", list(files))
    deadline = _time.time() + ready_timeout
    while _time.time() < deadline:
        _time.sleep(2)
        titles = nex_chip_titles()
        if len(titles) == len(files) and all(t and "ready to use" in t for t in titles):
            return titles
        if any(t and "can't be used" in t for t in titles):
            return titles
    return nex_chip_titles()


def nex_clear_composer():
    """Drafts persist per brand; a stale one silently prepends to the prompt."""
    from browser_harness.helpers import js, cdp, press_key
    # The placeholder differs between a new chat and an open one.
    ax_click("", role="textbox", settle=0.4)
    for _ in range(3):
        cdp("Input.dispatchKeyEvent", type="keyDown", modifiers=4, key="a",
            code="KeyA", windowsVirtualKeyCode=65, nativeVirtualKeyCode=65)
        cdp("Input.dispatchKeyEvent", type="keyUp", modifiers=4, key="a",
            code="KeyA", windowsVirtualKeyCode=65, nativeVirtualKeyCode=65)
        _time.sleep(0.2)
        press_key("Delete")
        _time.sleep(0.4)
        if not js("document.querySelector('[role=textbox]').innerText.trim()"):
            return True
    return False


def nex_send(prompt, settle=2.0):
    from browser_harness.helpers import js, cdp, drain_events
    nex_clear_composer()
    cdp("Input.insertText", text=prompt)
    _time.sleep(1.0)
    if nex_submit_disabled():
        raise RuntimeError("submit still disabled: " + str(nex_chip_titles()))
    drain_events()
    ax_click("Submit", role="button", settle=settle)


def nex_wait_turn(timeout=600, quiet_for=15):
    """Wait until the assistant is done; collect websocket frames meanwhile.

    Done means: no in-flight indicator AND the page text has stopped changing.
    Text alone is not enough — a long tool call keeps the page still.
    """
    from browser_harness.helpers import js, drain_events
    frames, last_len, last_change = [], -1, _time.time()
    deadline = _time.time() + timeout
    busy_words = ("Thinking", "Generating", "Working", "Reading", "Analyzing",
                  "Running", "Stop generating")
    while _time.time() < deadline:
        _time.sleep(3)
        for e in drain_events():
            m = e.get("method", "")
            if m in ("Network.webSocketFrameReceived", "Network.webSocketFrameSent"):
                frames.append((m, e["params"]["response"]["payloadData"]))
        txt = js("document.body.innerText")
        busy = any(w in txt for w in busy_words)
        if len(txt) != last_len:
            last_len, last_change = len(txt), _time.time()
        elif not busy and _time.time() - last_change > quiet_for:
            break
    return frames


def nex_tool_calls(frames):
    """Tool names seen in the agent websocket stream, in order."""
    import re
    names = []
    for _, payload in frames:
        for n in re.findall(r'"(?:toolName|tool_name|name)"\s*:\s*"([a-z_]+)"', payload):
            if n.endswith("attachment") or n.endswith("artifact") or "attachment" in n:
                names.append(n)
    return names


EVID = "/Users/max/Projects/About-Intelligence/nex-attachment-acceptance/2026-09-20/evidence/"


def nex_capture(name, prompt=None):
    """Persist a turn's tool blocks and reply text; return a short summary."""
    from browser_harness.helpers import js, page_info, capture_screenshot
    import json as _json
    blocks = _json.loads(js(
        """(()=>{const ds=[...document.querySelectorAll('details')];ds.forEach(d=>d.open=true);
           return JSON.stringify(ds.map(d=>d.innerText))})()"""))
    text = js("document.body.innerText")
    url = page_info()["url"]
    payload = {"name": name, "url": url, "prompt": prompt,
               "tool_blocks": blocks, "page_text": text}
    with open(EVID + name + ".json", "w") as fh:
        _json.dump(payload, fh, ensure_ascii=False, indent=1)
    capture_screenshot(EVID + name + ".png")
    return {"conversation": url.rsplit("/", 1)[-1], "tool_blocks": len(blocks),
            "chars": len(text)}


def nex_reply(marker):
    """The page text after the given prompt marker — i.e. the assistant's turn."""
    from browser_harness.helpers import js
    t = js("document.body.innerText")
    i = t.find(marker)
    return t[i:] if i >= 0 else t[-3000:]


def nex_run(name, files, prompt, wait=600):
    chips = nex_open_chat(files)
    bad = [c for c in chips if not c or "ready to use" not in c]
    if bad:
        return {"name": name, "blocked": chips}
    nex_send(prompt)
    nex_wait_turn(timeout=wait)
    summary = nex_capture(name, prompt)
    summary["name"] = name
    summary["chips"] = chips
    return summary


def nex_followup(prompt, wait=600):
    """Another turn in the conversation already open."""
    nex_send(prompt)
    nex_wait_turn(timeout=wait)


def nex_reload_current(settle=8):
    """Reload the open conversation: proves the turn survives a page refresh."""
    from browser_harness.helpers import goto_url, wait_for_load, page_info, cdp
    url = page_info()["url"]
    goto_url(url)
    wait_for_load()
    _time.sleep(settle)
    cdp("Network.enable")
    return url
