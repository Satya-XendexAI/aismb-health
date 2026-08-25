const chatBody  = document.getElementById("chatBody");
const textInput = document.getElementById("textInput");
const sendBtn   = document.getElementById("sendBtn");

function timeNow() {
  return new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

function escapeHtml(str) {
  const div = document.createElement("div");
  div.textContent = str;
  return div.innerHTML;
}

function formatText(str) {
  return escapeHtml(str).replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>");
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
  if (!text) return;

  addBubble(text, "out");
  textInput.value = "";
  showTyping();

  try {
    const res  = await fetch("/api/send", {
      method:  "POST",
      headers: { "Content-Type": "application/json" },
      body:    JSON.stringify({ text }),
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
