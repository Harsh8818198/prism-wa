"""The public pages Meta asks for before an app can go live: a home page that says what the service is and who runs it (Meta's crawler must get a 200 from the
app's Site URL, and the Tech Provider form wants a website showing the service), a privacy policy, and how to have data deleted.

They are plain, static and need no login or secret, so they are mounted whether or not the service-to-service API is. Every statement here describes what this
code does (tokens sealed in vault.py, rows isolated per workspace in db/migrations, `contacts.erase_contact`, `audit.KEEP_DAYS`); change the page when the code changes.
It was written by the engineers, not reviewed by a lawyer: the owner should have it reviewed before leaning on it as binding.
"""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.responses import HTMLResponse

UPDATED = "23 October 2026"
CONTACT = "contactus@alphakore.org"

_STYLE = ("body{font-family:system-ui,-apple-system,Segoe UI,sans-serif;max-width:46rem;margin:2.5rem auto;padding:0 1.2rem;line-height:1.6;color:#1d1d1f}"
          "h1{font-size:1.8rem;margin-bottom:.2rem}h2{font-size:1.15rem;margin-top:2rem}table{border-collapse:collapse;width:100%;margin:.8rem 0}"
          "td,th{border:1px solid #d6d6d6;padding:.45rem .6rem;text-align:left;vertical-align:top;font-size:.95rem}th{background:#f4f4f4}.meta{color:#666}")


def _page(title: str, body: str) -> str:
    return ('<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">'
            "<title>%s</title><style>%s</style></head><body>%s</body></html>" % (title, _STYLE, body))


ADDRESS = "33 NarHari Park, Vadodara, Gujarat, India"

HOME = _page("Prism WhatsApp platform by Alphakore", """
<h1>Prism WhatsApp platform</h1>
<p class="meta">Built and run by Alphakore, a software studio in Vadodara, India.</p>

<p>Prism lets a business use its <strong>own WhatsApp Business number</strong> to talk to its customers, from one place, with its whole team.
The business stays the owner of its WhatsApp account and its customer list.</p>

<h2>What a business can do with it</h2>
<ul>
<li><strong>Shared team inbox.</strong> Everyone answers customers from the same screen; chats can be assigned, tagged and noted.</li>
<li><strong>Message templates.</strong> Write the messages WhatsApp requires for starting a conversation, send them for approval and see their status.</li>
<li><strong>Campaigns.</strong> Send an approved template to a list of customers who have agreed to hear from you. A person who replies STOP is not messaged again.</li>
<li><strong>Automatic replies and flows.</strong> Welcome messages, away messages and simple step-by-step conversations.</li>
<li><strong>AI replies (optional).</strong> A business can let an AI assistant draft or send answers from its own knowledge base, and hand over to a person when it is unsure.</li>
<li><strong>Forms and a small shop (optional).</strong> Collect details inside WhatsApp and take orders with payment links.</li>
</ul>

<h2>How a business gets started</h2>
<ol>
<li>Get Prism and a licence from Alphakore.</li>
<li>Connect your own WhatsApp number through Meta&rsquo;s official sign-up (WhatsApp Embedded Signup). You sign in to your own Facebook account and approve exactly what Prism may do.</li>
<li>Send, reply and run campaigns from Prism. Meta bills you directly for WhatsApp conversations.</li>
</ol>

<h2>Your data</h2>
<p>Each business sees only its own data. Read the <a href="/privacy">privacy policy</a>, or see <a href="/data-deletion">how to have data deleted</a>.</p>

<h2>About Alphakore</h2>
<p>Alphakore builds software, automation and AI tools for businesses. Prism is our product.<br>
%(address)s<br>
<a href="mailto:%(contact)s">%(contact)s</a> &middot; <a href="https://alphakore.org">alphakore.org</a></p>
<p class="meta"><a href="/privacy">Privacy policy</a> &middot; <a href="/data-deletion">Data deletion</a></p>
""" % {"address": ADDRESS, "contact": CONTACT})


PRIVACY = _page("Prism WhatsApp platform: Privacy Policy", """
<h1>Privacy Policy: Prism WhatsApp platform</h1>
<p class="meta">Operated by Alphakore. Last updated %(updated)s.</p>

<h2>1. Who this is for, and who is responsible</h2>
<p>The Prism WhatsApp platform (the &ldquo;Service&rdquo;) lets a business (our &ldquo;Customer&rdquo;) use its own WhatsApp Business number to send and receive messages, manage
templates and campaigns, keep a shared inbox and run automations. We, Alphakore, run the Service.</p>
<p>For the Customer&rsquo;s own account details we decide how the data is used. For the Customer&rsquo;s contacts &mdash; the people who message, or are messaged by, a Customer &mdash; the Customer
decides whom to contact and why, and we process that data on the Customer&rsquo;s behalf and only to run the Service.</p>

<h2>2. What we hold</h2>
<table>
<tr><th>About</th><th>What</th></tr>
<tr><td>The Customer&rsquo;s account</td><td>The workspace name, the names and e-mail addresses of the team members who use it, the licence it is linked to, and its settings.</td></tr>
<tr><td>The Customer&rsquo;s WhatsApp connection</td><td>The WhatsApp Business Account id, the phone number id, the number and its display name, and the access token Meta issues when the Customer connects. The token is stored
encrypted (AES-256-GCM) and is never shown to anyone.</td></tr>
<tr><td>The Customer&rsquo;s contacts</td><td>A phone number or WhatsApp id, the name WhatsApp shows, any tags or details the Customer adds, and whether the person agreed to or opted out of messages.</td></tr>
<tr><td>Conversations</td><td>The messages exchanged (text, images, documents, locations, replies, template messages), when they were sent and whether they were delivered or read, and notes the team writes.</td></tr>
<tr><td>What the Customer builds</td><td>Message templates, campaigns, segments, automations, forms and what people fill in, and, if the Customer uses the shop, products, orders and payment status.</td></tr>
<tr><td>Activity</td><td>A log of what the Customer&rsquo;s team did in the workspace (kept for 400 days) and technical records needed to deliver messages and keep the Service running.</td></tr>
</table>
<p>We never see or store a Facebook or WhatsApp password. When a Customer connects a number, Meta asks the Customer to approve the access we request, and we receive only what they approve.</p>

<h2>2a. The Meta permissions we ask for, and why</h2>
<table>
<tr><th>Permission</th><th>Used only to</th></tr>
<tr><td>whatsapp_business_management</td><td>Read and manage the Customer&rsquo;s WhatsApp Business Account that they connect to us: its numbers, its message templates and its profile, and receive its events.</td></tr>
<tr><td>whatsapp_business_messaging</td><td>Send messages from the Customer&rsquo;s number to the people the Customer chooses, and receive the replies.</td></tr>
</table>

<h2>3. How we use it</h2>
<p>To connect the Customer&rsquo;s number; to send and receive messages; to show the shared inbox; to run templates, campaigns and automations the Customer sets up; to show reports; to keep the
Service secure and working; and to give support when the Customer asks. We do not sell data, we do not use message content for advertising, we do not use it to train AI models, and we do not
message a Customer&rsquo;s contacts for ourselves.</p>

<h2>4. Who else receives data</h2>
<ul>
<li><strong>Meta (WhatsApp).</strong> Messages travel through WhatsApp&rsquo;s Business Platform, under Meta&rsquo;s own terms and privacy policy.</li>
<li><strong>Render</strong> runs the Service and <strong>Supabase</strong> hosts its database. Both are in Singapore.</li>
<li><strong>An AI provider, only if the Customer turns on AI replies.</strong> The text of the conversation needed to write a reply is sent to the provider the Customer&rsquo;s workspace is set up with (by default Groq).</li>
<li><strong>Razorpay, only if the Customer uses the shop&rsquo;s payment links.</strong></li>
<li>An authority, when the law requires it.</li>
</ul>
<p>We share with no one else.</p>

<h2>5. Where it is kept and for how long</h2>
<p>The Service and its database run in Singapore. Data is kept while the Customer&rsquo;s workspace is active. After a Customer stops using the Service, or on request, we delete the workspace&rsquo;s data within
30 days. The team activity log is deleted after 400 days.</p>

<h2>6. Security</h2>
<p>Connections use HTTPS. Meta access tokens are encrypted. Events from Meta are accepted only if their signature is valid. Each workspace&rsquo;s data is kept apart from every other workspace&rsquo;s in the database,
and only the Alphakore staff who run the Service can reach the database.</p>

<h2>7. Your choices and rights</h2>
<p>If you are a Customer&rsquo;s contact you can ask to see, correct or delete your data, or stop messages: tell the business that messages you, or write to us at the address below and we will pass it on or act on it.
A person can stop a business&rsquo;s messages by replying STOP; the Service records the opt-out and stops sending that business&rsquo;s messages to their number. To have data deleted, see
<a href="/data-deletion">how to ask for deletion</a>.</p>

<h2>8. Children</h2>
<p>The Service is for businesses and is not directed at anyone under 18.</p>

<h2>9. Changes</h2>
<p>If we change this policy we change the date above and tell Customers directly when the change matters.</p>

<h2>10. Contact and grievances</h2>
<p>Alphakore &middot; <a href="mailto:%(contact)s">%(contact)s</a> &middot; <a href="https://alphakore.org">alphakore.org</a><br>
For a grievance write to the same address with &ldquo;Grievance&rdquo; in the subject line.</p>
""" % {"updated": UPDATED, "contact": CONTACT})


DELETION = _page("Prism WhatsApp platform: Deleting your data", """
<h1>Deleting your data</h1>
<p class="meta">Prism WhatsApp platform, operated by Alphakore. Last updated %(updated)s.</p>

<h2>If you are a business using the Service</h2>
<p>E-mail <a href="mailto:%(contact)s">%(contact)s</a> from the address on your account with the subject &ldquo;Delete my workspace&rdquo;, and give your workspace name and the WhatsApp number you connected.
We delete the workspace, its contacts, conversations, templates, campaigns and the stored Meta access token, and confirm by e-mail within 30 days.</p>
<p>To stop us holding the access token without deleting anything else, remove the app from your Facebook business settings (Business Settings &rarr; Integrations &rarr; Business apps); that removes our app&rsquo;s access to your account.</p>

<h2>If you are a person a business has messaged</h2>
<p>E-mail <a href="mailto:%(contact)s">%(contact)s</a> with the subject &ldquo;Delete my data&rdquo;, the phone number you use on WhatsApp, and the name of the business that messaged you if you know it.
We delete the conversations, messages, tags, consent records and form answers held under that number in every workspace we run, and confirm within 30 days. Orders a business has recorded stay as that business&rsquo;s
records, without the delivery address and note.</p>
<p>You can also ask the business itself, since it decides whom it messages. To stop a business&rsquo;s messages, reply STOP (or &ldquo;unsubscribe&rdquo; or &ldquo;cancel&rdquo;); the Service records it and sends nothing more from that business to your number.</p>

<h2>What we cannot delete</h2>
<p>Copies on your own phone and with Meta are outside the Service; Meta&rsquo;s privacy policy covers those.</p>

<p>Questions: <a href="mailto:%(contact)s">%(contact)s</a>. See also the <a href="/privacy">privacy policy</a>.</p>
""" % {"updated": UPDATED, "contact": CONTACT})


def mount(app: FastAPI) -> None:
    @app.get("/", response_class=HTMLResponse)
    def home():
        return HTMLResponse(HOME)

    @app.get("/privacy", response_class=HTMLResponse)
    def privacy():
        return HTMLResponse(PRIVACY)

    @app.get("/data-deletion", response_class=HTMLResponse)
    def data_deletion():
        return HTMLResponse(DELETION)
