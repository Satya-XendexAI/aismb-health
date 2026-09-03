let currentUser = null;   // { phone, name, role }

const loginOverlay = document.getElementById("loginOverlay");
const phoneUI      = document.getElementById("phoneUI");
const loginOptions = document.getElementById("loginOptions");
const roleBadge    = document.getElementById("roleBadge");
const chatBody     = document.getElementById("chatBody");
const textInput    = document.getElementById("textInput");
const sendBtn      = document.getElementById("sendBtn");
const micBtn       = document.getElementById("micBtn");

// ── Login ──────────────────────────────────────────────────────────────

const ROLE_LABEL = { admin: "Admin", doctor: "Doctor", patient: "Patient" };
const ROLE_COLOR = { admin: "#d32f2f", doctor: "#1565c0", patient: "#2e7d32" };
const ROLE_ICON  = { admin: "🔑", doctor: "👨‍⚕️", patient: "🙋" };

async function loadLoginOptions() {
  try {
    const res  = await fetch("/api/config");
    const data = await res.json();
    data.users.forEach(user => {
      const btn = document.createElement("button");
      btn.className = "login-option-btn";
      btn.style.borderColor = ROLE_COLOR[user.role];
      btn.innerHTML = `
        <span class="login-option-icon">${ROLE_ICON[user.role]}</span>
        <span class="login-option-info">
          <strong>${user.name}</strong>
          <small>${ROLE_LABEL[user.role]} · ${user.phone}</small>
        </span>`;
      btn.addEventListener("click", () => startSession(user));
      loginOptions.appendChild(btn);
    });
  } catch (e) {
    loginOptions.innerHTML = "<p style='color:#999'>Could not load config</p>";
  }
}

document.getElementById("customBtn").addEventListener("click", () => {
  const phone = document.getElementById("customNumber").value.trim();
  if (!phone) return;
  startSession({ phone, name: "Patient", role: "patient" });
});

function startSession(user) {
  currentUser = user;
  loginOverlay.style.display = "none";
  phoneUI.style.display      = "flex";
  roleBadge.textContent      = `${ROLE_ICON[user.role]} ${ROLE_LABEL[user.role]}`;
  roleBadge.style.background = ROLE_COLOR[user.role];
  textInput.focus();
}

loadLoginOptions();

// ── Chat ──────────────────────────────────────────────────────────────

function timeNow() {
  return new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

function escapeHtml(str) {
  const div = document.createElement("div");
  div.textContent = str;
  return div.innerHTML;
}

function formatText(str) {
  return escapeHtml(str)
    .replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>")
    .replace(/\*(.+?)\*/g,     "<strong>$1</strong>")
    .replace(/\n/g,            "<br>");
}

function addBubble(text, direction) {
  const bubble = document.createElement("div");
  bubble.className = `bubble ${direction}`;
  bubble.innerHTML = `${formatText(text)}<span class="time">${timeNow()}</span>`;
  chatBody.appendChild(bubble);
  chatBody.scrollTop = chatBody.scrollHeight;
}

function showTyping() {
  const el = document.createElement("div");
  el.className = "typing";
  el.id = "typingIndicator";
  el.textContent = "typing…";
  chatBody.appendChild(el);
  chatBody.scrollTop = chatBody.scrollHeight;
}

function hideTyping() {
  const el = document.getElementById("typingIndicator");
  if (el) el.remove();
}

async function sendMessage() {
  const text = textInput.value.trim();
  if (!text || !currentUser) return;

  addBubble(text, "out");
  textInput.value = "";
  showTyping();

  try {
    const res  = await fetch("/api/send", {
      method:  "POST",
      headers: { "Content-Type": "application/json" },
      body:    JSON.stringify({ text, from_number: currentUser.phone }),
    });
    const data = await res.json();
    hideTyping();
    data.replies.forEach((reply) => addBubble(reply, "in"));
  } catch (err) {
    hideTyping();
    addBubble("[Error contacting server]", "in");
  }
}

sendBtn.addEventListener("click", sendMessage);
textInput.addEventListener("keydown", (e) => {
  if (e.key === "Enter") sendMessage();
});

// ── Voice messages ───────────────────────────────────────────────────────

let mediaRecorder = null;
let audioChunks    = [];
let isRecording     = false;

async function toggleRecording() {
  if (!currentUser) return;

  if (!isRecording) {
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      audioChunks   = [];
      mediaRecorder = new MediaRecorder(stream);
      mediaRecorder.ondataavailable = (e) => audioChunks.push(e.data);
      mediaRecorder.onstop = sendRecordedAudio;
      mediaRecorder.start();
      isRecording = true;
      micBtn.classList.add("recording");
    } catch (err) {
      addBubble("[Microphone access denied]", "in");
    }
  } else {
    mediaRecorder.stop();
    mediaRecorder.stream.getTracks().forEach((track) => track.stop());
    isRecording = false;
    micBtn.classList.remove("recording");
  }
}

async function sendRecordedAudio() {
  const blob = new Blob(audioChunks, { type: mediaRecorder.mimeType || "audio/webm" });
  addBubble("🎤 Voice message", "out");
  showTyping();

  const form = new FormData();
  form.append("from_number", currentUser.phone);
  form.append("audio", blob, "voice.webm");

  try {
    const res  = await fetch("/api/send-audio", { method: "POST", body: form });
    const data = await res.json();
    hideTyping();
    data.replies.forEach((reply) => addBubble(reply, "in"));
  } catch (err) {
    hideTyping();
    addBubble("[Error contacting server]", "in");
  }
}

micBtn.addEventListener("click", toggleRecording);
