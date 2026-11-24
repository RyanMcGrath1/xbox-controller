const $ = (id) => document.getElementById(id);

let state = { connected: false, consoles: [], selected: null };
let toastTimer = 0;
let powerArmed = false;
let seeking = false;
let stickChain = Promise.resolve();
let inputChain = Promise.resolve();

const KEYS = {
  ArrowUp: "up",
  ArrowDown: "down",
  ArrowLeft: "left",
  ArrowRight: "right",
  a: "a",
  b: "b",
  x: "x",
  y: "y",
  A: "a",
  B: "b",
  X: "x",
  Y: "y",
  q: "lb",
  e: "rb",
  Q: "lb",
  E: "rb",
  Enter: "a",
  Backspace: "b",
  " ": "nexus",
  Shift: "view",
  Escape: "menu",
};

function toast(text) {
  const node = $("toast");
  node.textContent = text;
  node.hidden = !text;
  clearTimeout(toastTimer);
  if (text) toastTimer = setTimeout(() => { node.hidden = true; }, 4200);
}

async function api(path, body) {
  const response = await fetch(path, {
    method: body ? "POST" : "GET",
    headers: body ? { "Content-Type": "application/json" } : undefined,
    body: body ? JSON.stringify(body) : undefined,
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    const error = new Error(data.detail || "Request failed");
    error.payload = data;
    throw error;
  }
  return data;
}

function clock(microseconds) {
  const total = Math.max(0, Math.round(Number(microseconds || 0) / 1_000_000));
  const minutes = Math.floor(total / 60);
  const seconds = String(total % 60).padStart(2, "0");
  return `${minutes}:${seconds}`;
}

function render(next) {
  state = next;
  const consoleInfo = state.console;
  const selected = (state.consoles || []).find((item) => item.selected) || consoleInfo;
  $("console-name").textContent = (consoleInfo && consoleInfo.name) || (selected && selected.name) || "No Xbox yet";
  const title = state.status && state.status.title;
  $("now-title").textContent = state.connected
    ? (title ? title.name : "Dashboard")
    : "Same Wi-Fi as the console";
  $("led").classList.toggle("on", !!state.connected);

  const banner = state.error || state.message || "";
  $("banner").hidden = !banner;
  $("banner").textContent = banner;

  const media = state.media;
  $("media-title").textContent = media ? media.title : "No media reported";
  $("media-sub").textContent = media
    ? [media.subtitle, media.duration ? `${clock(media.position)} / ${clock(media.duration)}` : ""].filter(Boolean).join(" · ")
    : "";
  $("playback").textContent = media && media.playback ? media.playback : "Idle";
  const seek = $("seek");
  seek.disabled = !(media && media.duration);
  if (!seeking && media && media.duration) {
    seek.value = String(Math.round((media.position / media.duration) * 1000));
  }
  $("text-prompt").textContent = state.text_prompt
    ? `Xbox is asking: ${state.text_prompt}`
    : "Open a text field on the Xbox, then send from here.";

  const list = $("consoles");
  const signature = JSON.stringify(state.consoles || []);
  if (list.dataset.signature !== signature) {
    list.dataset.signature = signature;
    list.replaceChildren();
    if (!state.consoles || !state.consoles.length) {
      const empty = document.createElement("li");
      empty.className = "empty";
      empty.textContent = "None saved yet. Tap Find while the Xbox is on.";
      list.appendChild(empty);
    } else {
      for (const item of state.consoles) {
        const row = document.createElement("li");
        const copy = document.createElement("div");
        const name = document.createElement("strong");
        name.textContent = item.name;
        const meta = document.createElement("span");
        const bits = [item.connected ? "Connected" : item.available ? "On" : "Asleep"];
        if (item.address) bits.push(item.address);
        meta.textContent = bits.join(" · ");
        copy.append(name, meta);
        const action = document.createElement("button");
        action.type = "button";
        action.textContent = item.connected ? "On" : item.available ? "Connect" : "Wake";
        action.addEventListener("click", () => choose(item));
        row.append(copy, action);
        list.appendChild(row);
      }
    }
  }
}

async function refresh() {
  try {
    render(await api("/api/status"));
  } catch (error) {
    toast(error.message);
  }
}

async function choose(item) {
  try {
    if (item.available || item.connected) render(await api("/api/connect", { liveid: item.liveid }));
    else render(await api("/api/power/on", { liveid: item.liveid }));
  } catch (error) {
    toast(error.message);
  }
}

async function ensure() {
  if (state.connected) return;
  if (!state.selected) {
    const found = await api("/api/discover", {});
    render(found);
  }
  render(await api("/api/connect", { liveid: state.selected }));
}

function sendInput(name, action) {
  inputChain = inputChain.then(async () => {
    if (action === "down") await ensure();
    await api("/api/input", { button: name, action });
  }).catch((error) => toast(error.message));
}

function bindHold(button, name) {
  const down = (event) => {
    event.preventDefault();
    button.setPointerCapture?.(event.pointerId);
    button.classList.add("is-down");
    if (navigator.vibrate) navigator.vibrate(8);
    sendInput(name, "down");
  };
  const up = () => {
    button.classList.remove("is-down");
    sendInput(name, "up");
  };
  button.addEventListener("pointerdown", down);
  button.addEventListener("pointerup", up);
  button.addEventListener("pointercancel", up);
  button.addEventListener("contextmenu", (event) => event.preventDefault());
}

document.querySelectorAll("[data-button]").forEach((button) => {
  bindHold(button, button.dataset.button);
});

document.querySelectorAll("[data-stick]").forEach((node) => {
  const well = node.querySelector(".well");
  const knob = node.querySelector(".knob");
  const name = node.dataset.stick;
  const move = (event) => {
    const rect = well.getBoundingClientRect();
    const radius = rect.width / 2;
    let x = (event.clientX - rect.left - radius) / (radius * 0.72);
    let y = (rect.top + radius - event.clientY) / (radius * 0.72);
    const length = Math.hypot(x, y) || 1;
    if (length > 1) {
      x /= length;
      y /= length;
    }
    knob.style.transform = `translate(${x * radius * 0.55}px, ${-y * radius * 0.55}px)`;
    stickChain = stickChain
      .then(() => api("/api/stick", { stick: name, x, y }))
      .catch((error) => toast(error.message));
  };
  const end = () => {
    knob.style.transform = "translate(0, 0)";
    api("/api/stick", { stick: name, x: 0, y: 0 }).catch(() => {});
  };
  well.addEventListener("pointerdown", (event) => {
    well.setPointerCapture(event.pointerId);
    ensure().then(() => move(event)).catch((error) => toast(error.message));
  });
  well.addEventListener("pointermove", (event) => {
    if (well.hasPointerCapture(event.pointerId)) move(event);
  });
  well.addEventListener("pointerup", end);
  well.addEventListener("pointercancel", end);
});

document.querySelectorAll("[data-media]").forEach((button) => {
  button.addEventListener("click", async () => {
    try {
      await ensure();
      render(await api("/api/media", { command: button.dataset.media }));
    } catch (error) {
      toast(error.message);
    }
  });
});

$("seek").addEventListener("pointerdown", () => { seeking = true; });
$("seek").addEventListener("change", async () => {
  seeking = false;
  if (!state.media || !state.media.duration) return;
  const position = Math.round((Number($("seek").value) / 1000) * state.media.duration);
  try {
    render(await api("/api/media", { command: "seek", position }));
  } catch (error) {
    toast(error.message);
  }
});

document.querySelectorAll("[data-record]").forEach((button) => {
  button.addEventListener("click", async () => {
    try {
      await ensure();
      const next = await api("/api/record", { seconds: Number(button.dataset.record) });
      render(next);
      toast(next.message || "Clip requested");
    } catch (error) {
      toast(error.message);
    }
  });
});

$("text-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const input = $("text");
  try {
    await ensure();
    render(await api("/api/text", { text: input.value }));
    input.value = "";
  } catch (error) {
    toast(error.message);
  }
});

$("launch").addEventListener("submit", async (event) => {
  event.preventDefault();
  try {
    await ensure();
    const next = await api("/api/launch", { uri: $("uri").value });
    render(next);
    toast(next.message || "Launch sent");
  } catch (error) {
    toast(error.message);
  }
});

$("discover").addEventListener("click", async () => {
  $("discover").textContent = "Finding";
  try {
    const found = await api("/api/discover", {});
    render(found);
    const online = (found.consoles || []).filter((item) => item.available);
    if (online.length === 1) render(await api("/api/connect", { liveid: online[0].liveid }));
    else if (!online.length) toast("No Xbox answered. Wake it with Power if you have seen it before.");
  } catch (error) {
    toast(error.message);
  } finally {
    $("discover").textContent = "Find";
  }
});

$("manual").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  try {
    render(await api("/api/remember", {
      name: form.name.value,
      address: form.address.value,
      liveid: form.liveid.value,
    }));
    toast("Saved");
    form.reset();
  } catch (error) {
    toast(error.message);
  }
});

$("power").addEventListener("click", async () => {
  try {
    const awake = state.connected || (state.consoles || []).some((item) => item.selected && item.available);
    if (awake) {
      if (!powerArmed) {
        powerArmed = true;
        $("power").classList.add("armed");
        toast("Tap power again to turn the Xbox off");
        setTimeout(() => {
          powerArmed = false;
          $("power").classList.remove("armed");
        }, 2500);
        return;
      }
      powerArmed = false;
      $("power").classList.remove("armed");
      render(await api("/api/power/off", {}));
      return;
    }
    render(await api("/api/power/on", { liveid: state.selected }));
  } catch (error) {
    toast(error.message);
  }
});

const heldKeys = new Set();
window.addEventListener("keydown", (event) => {
  if (event.target.matches("input, textarea") || event.metaKey || event.ctrlKey || event.altKey) return;
  const button = KEYS[event.key];
  if (!button || event.repeat || heldKeys.has(event.key)) return;
  event.preventDefault();
  heldKeys.add(event.key);
  sendInput(button, "down");
});
window.addEventListener("keyup", (event) => {
  const button = KEYS[event.key];
  if (!button) return;
  heldKeys.delete(event.key);
  sendInput(button, "up");
});
window.addEventListener("blur", () => {
  for (const key of heldKeys) {
    const button = KEYS[key];
    if (button) sendInput(button, "up");
  }
  heldKeys.clear();
});

refresh();
