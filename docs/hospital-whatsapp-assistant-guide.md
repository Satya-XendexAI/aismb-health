# The Hospital WhatsApp Assistant

*A plain-language guide to what this system does, how it behaves, and what a patient or a
doctor actually experiences when they message the hospital on WhatsApp — written without
engineering jargon, for anyone on the team to read end to end.*

**At a glance:** 2 user roles · 3 capabilities live today · 1 confirmation always required before
anything is booked · 6 more services already designed and approved for later.

---

## 1. The big idea

It's a member of hospital staff that never sleeps, lives entirely inside WhatsApp, and can hold a
real conversation. A patient texts the hospital's WhatsApp number the way they'd text a friend —
describing a symptom, asking about a doctor, asking to book a visit — and the assistant reads the
message, decides what the person actually needs, and either answers directly or takes an action on
their behalf, like reserving a place in a doctor's queue.

Separately, the *same* WhatsApp number recognizes when a message is coming from a member of the
hospital's **medical staff** rather than a patient, and switches into a completely different mode
for them — one built for looking things up, not for booking anything.

Nothing about this requires a separate app, a login, or a form. The entire experience is a text
conversation, which is why almost every feature in this guide is described as a back-and-forth
exchange of messages rather than screens or buttons.

---

## 2. How a message travels through the system

You don't need to know any of the technical machinery to use this guide, but it helps to see the
shape of the journey once. Every message — in either direction — passes through the same five
stops:

1. **Arrives** — Patient or doctor sends a WhatsApp message
2. **Listens** — The hospital's WhatsApp line receives it
3. **Thinks** — The AI assistant decides what's needed
4. **Acts** — It looks something up or performs an action
5. **Replies** — An answer is sent back on WhatsApp

Step 3 — "thinks" — is the heart of the system. It's an AI language model (the same category of
technology behind well-known chat assistants) that has been given a very specific job description
and a very specific, limited set of actions it's allowed to take. It cannot do anything outside
that list. It cannot, for instance, invent a doctor that doesn't exist, or make up a queue number —
every fact it states about a doctor, an appointment, or hospital data comes from actually looking
it up, never from guessing.

The assistant also keeps a short memory of the current conversation, so it doesn't ask a patient
for their name twice in the same chat, and it remembers whether it's still waiting on a yes/no
confirmation from earlier in the exchange. More on that in [Conversation memory](#10-how-the-assistant-remembers-a-conversation).

---

## 3. Two kinds of users, two different assistants

The very first thing the system does with any message is work out *who* is sending it. It keeps a
small internal roster of the WhatsApp numbers that belong to medical staff. If the sender's number
is on that roster, they're treated as a **doctor** for the rest of the conversation. Every other
number is treated as a **patient** by default — no sign-up needed.

That single decision changes almost everything about how the assistant behaves next: what it's
allowed to talk about, what actions it's allowed to take, and even what tone it uses.

### Patient

Anyone texting in who isn't recognized as staff. Can describe symptoms, ask about doctors or
departments, and book or cancel a queue place for themselves.

- Gets a warm, caring conversational tone
- Can search for doctors and ask general questions
- Can book or cancel — always with a confirmation step first
- Cannot see any hospital data belonging to other patients

### Doctor

A recognized member of medical staff. Gets a brisker, work-tool tone focused on lookups rather
than conversation.

- Can search the doctor directory, same as a patient can
- Can ask plain-English questions about their own patients, tokens, and records
- Cannot book or cancel an appointment through the assistant — that stays a patient action
- Can only ever see data tied to themselves, never another doctor's patients

---

## 4. A patient's conversation, start to finish

This is the most important flow in the whole system, so it's worth walking through carefully.
Here's what happens when someone messages the hospital describing a health concern.

1. **A concern comes in.** The patient sends something like "I've had chest pain since this
   morning." The assistant responds with one sentence of empathy, and in that same reply asks for
   their name, how long the symptom has lasted, and whether anything else is going on — all in one
   natural message, not a form.

2. **It checks for anything urgent, first.** Before doing anything else, the assistant weighs
   whether the description sounds like a genuine emergency — severe or sudden chest pain, pain
   spreading to the arm or jaw, trouble breathing, signs of a stroke, heavy bleeding, loss of
   consciousness, or a high fever with a stiff neck.

   > **If it looks like an emergency**, the assistant does not try to book anything or list
   > doctors. It immediately tells the patient this looks like a medical emergency and to go to
   > the Emergency Department right away — and the conversation stops there. Getting the person to
   > real, immediate help always outranks the booking flow.

3. **Otherwise, it finds the right doctors.** For anything that isn't urgent, the assistant
   searches the hospital's full doctor directory for specialists that match the symptom, and
   presents a short, clear list — name, specialty, years of experience, and consultation fee —
   then pauses and waits for the patient to respond, rather than pushing straight into booking.

   > **Assistant:** "That sounds uncomfortable, and I'm sorry you're dealing with it. Could I get
   > your name, and how long you've had the pain — has anything else been bothering you along with
   > it? Based on what you've shared, here are two cardiologists available today: Dr. A (14 yrs
   > experience, ₹600) and Dr. B (9 yrs, ₹450)."

4. **The patient asks to book.** Once the patient names a doctor and asks to go ahead, the
   assistant already has their name and symptoms from earlier in the conversation, so it won't ask
   again. It may still ask for anything it's missing — preferred date, age, or location — but
   never repeats a question it already has the answer to.

5. **Nothing books itself — a confirmation always comes first.** Before the booking actually
   happens, the assistant stops and sends a plain-language confirmation: "Please confirm: BOOK
   appointment with Dr. A (Cardiology) for Rohan on 2026-08-25. Reply YES to proceed or NO to
   cancel." Nothing is written to the hospital's records until the patient replies.

6. **A "yes" locks in a real queue ticket.** Once confirmed, the patient receives an actual queue
   number for that doctor's day — see [The queue & booking system](#8-how-the-queue--booking-system-works)
   for exactly how that number and its estimated time are worked out. A "no," or anything that
   isn't a clear yes, simply cancels the request with nothing recorded.

Two smaller touches worth knowing about: in the very first couple of messages of a brand-new
conversation, the assistant deliberately keeps the option to book out of its own hands — it
focuses purely on understanding the person, not steering them toward a booking, unless they've
clearly said they want to book from the start. And if a reply would come out very long, it's
automatically split into a few shorter messages, the way a person might send several texts in a
row rather than one wall of text.

---

## 5. A doctor's conversation, start to finish

When the sender's number matches someone on the medical staff roster, the entire personality and
purpose of the assistant changes — from a caring intake conversation to a fast internal lookup
tool.

1. **The assistant introduces a different kind of help.** There's no empathetic small talk, no
   symptom triage. The assistant is ready to search the doctor directory, or to answer questions
   about the doctor's own patients, appointments, and records.

2. **Directory lookups work the same as for patients.** A doctor can ask things like "who else
   here treats pediatric patients?" or "any dermatologists who speak Tamil?" and get the same kind
   of directory search a patient would.

3. **Plain-English questions become safe data lookups.** This is the doctor-only capability: a
   doctor can ask something like "how many patients do I have waiting today?" or "what's the phone
   number for token 5?" in ordinary English, and the assistant translates that into a precise, safe
   lookup against the hospital's records — see [The assistant's toolbox](#7-the-assistants-toolbox)
   for how that translation is kept safe.

   > **Doctor:** "How many of my patients today are still waiting?"
   > **Assistant:** "You have 6 patients currently waiting, tokens 3 through 8."

4. **Booking and cancelling are deliberately out of reach.** Even if a doctor asked the assistant
   to book or cancel something, it's simply not offered to them as a possible action — that
   capability belongs to the patient conversation only, by design, so that a patient's own booking
   always reflects what the patient actually asked for.

---

## 6. Safety & permissions built into every step

Because this assistant can take real actions — not just answer questions — a lot of care has gone
into making sure it can never do something a person didn't actually ask for, or see something it
shouldn't. These protections aren't optional add-ons; they sit directly in the path of every
message.

- **Confirm before you commit.** Booking or cancelling a queue place is the *only* kind of action
  the assistant can take that changes hospital records, and it is never allowed to do so silently.
  It always pauses, describes exactly what it's about to do in plain language, and waits for an
  explicit "yes" or "no" first.
- **Role-based limits.** Every capability the assistant has is explicitly tied to who's allowed to
  use it. Booking is patient-only. The data-lookup question tool is doctor-only. If a request falls
  outside what someone's role is allowed to do, the assistant declines and explains that politely
  rather than attempting it.
- **A doctor only ever sees their own patients.** When a doctor asks a data question, the lookup is
  automatically restricted to records tied to that specific doctor. There's no way for one doctor's
  question to surface another doctor's patient information — the restriction is enforced on every
  single lookup, not just assumed.
- **No made-up medical answers.** The assistant is explicitly instructed to never fabricate
  information about doctors, availability, or hospital data — everything it states as fact has to
  come from an actual lookup, never from the AI simply guessing a plausible-sounding answer.
- **Read-only data questions, double-checked.** When a doctor's plain-English question is turned
  into a database lookup, the system technically cannot produce anything that changes records —
  only ever a read. It's also automatically re-checked a second time before running, to catch and
  fix a lookup that referenced something that doesn't actually exist.
- **Genuine emergencies bypass everything else.** As covered in the patient journey, anything that
  reads as a possible medical emergency short-circuits the entire booking conversation and sends
  the patient straight to an instruction to seek emergency care immediately — no doctor list, no
  booking offer, no delay.

---

## 7. The assistant's toolbox

The AI never has free rein — it's given a short, explicit list of actions it's allowed to take, and
it must pick from that list rather than doing anything else. Today there are exactly three:

| # | Tool | Who can use it | What it does |
|---|------|-----------------|--------------|
| 1 | **Find a doctor** | Patient & Doctor | Searches the full hospital doctor directory by symptom, specialty, name, spoken language, or years of experience, and returns a clear shortlist. See [The doctor directory](#9-the-doctor-directory-and-how-its-searched). |
| 2 | **Book or cancel a queue place** | Patient only | Reserves — or releases — a place in a specific doctor's queue for a specific day. Always gated behind the yes/no confirmation described in [Safety & permissions](#6-safety--permissions-built-into-every-step). |
| 3 | **Answer a data question** | Doctor only | Turns a doctor's plain-English question about their own patients or tokens into a safe, read-only lookup against the hospital's records, automatically scoped to that one doctor, and checked twice before it runs. |

Six further tools have already been designed and approved for future rollout — see
[Live today vs. on the roadmap](#12-live-today-versus-already-designed-for-later) for what they'll
add.

---

## 8. How the queue & booking system works

Appointments in this system aren't booked into fixed time slots — they work like a take-a-number
queue at a clinic. Every doctor has one running queue per day, and booking simply reserves the next
number in that line.

### What happens when someone books

The assistant checks that the hospital and the doctor both exist and are active, finds or creates
that doctor's queue for the requested day, and confirms the patient doesn't already have an open
ticket with that same doctor (to stop accidental double-bookings). It then hands out the next queue
number in line and replies with a confirmation that includes:

- The **queue (token) number** assigned
- The doctor's name, department, and the hospital's name and address
- The consultation fee, if one is set
- An **estimated time** the patient is likely to be seen

### How the estimated time is worked out

The estimate is simple and honest, not a guess: it starts from either the time the doctor's session
actually started that day (if it has), or the doctor's usual check-in time otherwise, and then adds
a few minutes for every patient who is still ahead in the queue, based on how long that doctor
typically spends per patient. If four people are ahead and the doctor typically takes ten minutes
each, the new patient can expect roughly forty minutes past that starting point.

### Cancelling works the same way, in reverse

A cancellation request looks the patient up by their phone number, finds their currently active
queue ticket with that doctor, and marks it cancelled — freeing up that place. If there's no
matching patient record or no active ticket to cancel, the assistant says so plainly rather than
pretending something was cancelled.

### What keeps this reliable

Every hospital record — patients, doctors, daily queues, and individual tickets — lives in the
hospital's permanent database, the same one used across the hospital's other systems, not somewhere
private to the chat assistant. Queue numbers are also handed out one at a time in strict order, so
two people booking at the exact same moment can never accidentally be given the same number.

---

## 9. The doctor directory, and how it's searched

Behind "find me a cardiologist" or "who treats knee pain" sits a proper directory of every doctor
in the hospital's network — their specialties, hospital and city, languages spoken, years of
experience, and consultation fees — built so it can be searched the way a person actually asks, not
just by exact keyword.

### It searches more than one way, then blends the results

When someone describes a symptom in their own words — "my knee's been hurting since I fell" — the
assistant doesn't need them to know the medical term. It first has a small, fast AI step translate
common symptom language into the right medical specialty (knee and joint pain, for example, maps to
Orthopaedics). It then searches the directory two different ways at once — a straightforward
keyword match, and a "meaning" match that can find relevant doctors even when the wording doesn't
line up exactly — and combines both sets of results into a single, best-ranked shortlist, rather
than relying on just one method.

### What a patient or doctor actually sees

The shortlist that comes back is trimmed to a handful of the strongest matches and formatted
plainly: each doctor's name, title, specialty, years of experience, languages, hospital, and fee —
plus, where the information is available, the very next open slot for that doctor, so the reply is
genuinely useful rather than just a list of names.

---

## 10. How the assistant remembers a conversation

A good assistant shouldn't ask the same question twice, and shouldn't forget what it was waiting
on. Two different kinds of "memory" make that possible, and it's worth keeping them distinct.

**Permanent hospital records** — Patients, doctors, daily queues, and every ticket that's ever been
booked or cancelled live in the hospital's real, permanent database — the same lasting record used
elsewhere in the hospital. This is never lost and isn't specific to the chat assistant.

**The current chat, in progress** — While a conversation is actively happening, the assistant keeps
a short working memory of the last several messages back and forth, plus whether it's mid-way
through waiting for a yes/no confirmation. This is what lets it avoid re-asking a patient's name it
already collected earlier in the same chat.

That short-term conversation memory is intentionally kept small — only the most recent handful of
exchanges — so replies stay fast and focused, and it's separate from anything that actually gets
permanently recorded about a patient's visit.

---

## 11. How the team tests conversations before they go live

Two lightweight tools exist purely so the development team can have a full conversation with the
assistant without needing a real WhatsApp number, a real patient, or spending anything on actual
messages. Neither is something a patient or doctor would ever see.

**A look-alike chat screen** — A simple web page, styled to look like a WhatsApp conversation, that
talks to the exact same assistant real patients use. It lets someone on the team try out symptoms,
bookings, and doctor searches in a browser and see typing indicators and replies exactly as a
patient would.

**A command-line test conversation** — A more bare-bones version of the same idea for quick checks
from a terminal — including handy commands to switch between a "patient" number and a known
"doctor" number, check the current state of a conversation, or wipe a test conversation clean and
start over.

---

## 12. Live today, versus already designed for later

Every capability described above — finding a doctor, booking or cancelling a queue place, and a
doctor's data questions — is real and working today. Beyond that, six further patient-facing
services have already been thought through in detail and formally approved for building, but
haven't been built into the assistant yet. Sharing them here so the team knows the intended
direction, not just the current state.

| Service | Status | What it will add |
|---|---|---|
| Lab test & scan reminders | Planned | Proactive nudges when a lab test or scan is coming up or overdue. |
| Prescription reminders | Planned | Gentle WhatsApp reminders to take or refill ongoing medication. |
| Post-visit follow-up | Planned | A check-in message after a visit to see how the patient is doing. |
| Patient feedback collection | Planned | A simple way to gather a patient's rating and comments after care. |
| Healthcare tips & wellness | Planned | General wellness and prevention tips sent through the same chat. |
| Insurance queries | Planned | Answering patient questions about coverage and insurance directly. |

One more thing worth knowing: every capability in this system — built or planned — starts as a
written design document that the team reviews and approves before any of it is built. That's also
how this guide was able to be this precise about what's real versus what's coming.

---

## 13. Glossary

**AI assistant / AI language model** — The technology that reads a message and decides, in the
moment, what it means and how to respond — the same general kind of technology behind well-known
consumer chat assistants, here given a hospital-specific job and a strict, limited list of actions
it's allowed to take.

**Queue place / token** — A numbered place in a doctor's line for the day — like a take-a-number
ticket — rather than a booked time slot. The number tells a patient roughly where they stand.

**Confirmation step** — The mandatory pause, before any booking or cancellation, where the
assistant describes exactly what it's about to do and waits for an explicit yes or no.

**Role** — Whether the person messaging is treated as a patient or as recognized medical staff —
decided automatically from their WhatsApp number, and used to control what they're allowed to do.

**Doctor directory** — The hospital's full, searchable listing of doctors — specialty, experience,
languages, hospital, and fee — that both patients and doctors can search through the assistant.

**Data question** — A plain-English question a doctor can ask about their own patients or queue,
which the assistant safely turns into a precise lookup rather than a guess.

**Conversation memory** — The short, temporary record of what's been said so far in one active chat
— separate from the hospital's permanent records — that lets the assistant avoid repeating
questions.

---

*Written as a plain-language walkthrough of the current codebase for non-technical teammates.
Reflects the system as built as of 25 August 2026.*
