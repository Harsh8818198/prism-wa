"""Ready-made flows, to copy and change (spec WS8, "library"): a construction and trade business first, then thirty more by topic.

Each entry is a complete, valid flow: a trigger and a graph. The owner picks one, edits the wording and the
buttons to suit, publishes it and switches it on. Every entry is checked by a test, so a library flow can never
be one that Prism itself would refuse to publish.
"""
from __future__ import annotations

LIBRARY = [
    {
        "key": "lead_capture", "title": "Capture a new enquiry",
        "description": "Greets a new customer, asks what they need, takes their name and city, and passes them to your team.",
        "trigger": {"type": "first_message"},
        "graph": {"entry": "greet", "nodes": {
            "greet": {"type": "buttons", "text": "Hello {{first_name}}! Thanks for contacting us. What do you need today?",
                      "options": [{"id": "quote", "title": "Get a quote", "next": "ask_name"},
                                  {"id": "visit", "title": "Book a site visit", "next": "ask_name"},
                                  {"id": "talk", "title": "Talk to someone", "next": "person"}]},
            "ask_name": {"type": "ask", "text": "Great. What is your name?", "save_as": "attr:FullName", "next": "ask_city"},
            "ask_city": {"type": "ask", "text": "Which city is the project in?", "save_as": "attr:City", "next": "tag"},
            "tag": {"type": "add_tag", "tag": "new-enquiry", "next": "done"},
            "done": {"type": "handoff", "text": "Thank you {{attr.FullName}}. Our team will message you shortly.", "reason": "new enquiry"},
            "person": {"type": "handoff", "text": "Connecting you to our team now.", "reason": "asked for a person"}}},
    },
    {
        "key": "faq_menu", "title": "Answer common questions",
        "description": "A menu of the questions customers ask most. Change the answers to your own.",
        "trigger": {"type": "keyword", "match": "exact", "keywords": ["menu", "help", "info"]},
        "graph": {"entry": "menu", "nodes": {
            "menu": {"type": "list", "text": "What would you like to know?", "button_label": "Choose a topic",
                     "rows": [{"id": "hours", "title": "Opening hours", "next": "hours"},
                              {"id": "where", "title": "Where are you?", "next": "where"},
                              {"id": "pay", "title": "Payment options", "next": "pay"},
                              {"id": "person", "title": "Speak to a person", "next": "person"}]},
            "hours": {"type": "send_text", "text": "We are open Monday to Saturday, 10 am to 6 pm.", "next": "menu"},
            "where": {"type": "send_text", "text": "Our office is at <your address>. Send us a message before you visit.", "next": "menu"},
            "pay": {"type": "send_text", "text": "We accept UPI and bank transfer. A 30% advance confirms an order.", "next": "menu"},
            "person": {"type": "handoff", "text": "Connecting you to our team now.", "reason": "asked for a person"}}},
    },
    {
        "key": "site_visit", "title": "Book a site visit",
        "description": "Asks for a date and a time, saves them on the contact and hands over to confirm.",
        "trigger": {"type": "keyword", "match": "contains", "keywords": ["site visit", "visit"]},
        "graph": {"entry": "intro", "nodes": {
            "intro": {"type": "send_text", "text": "Happy to arrange a site visit.", "next": "date"},
            "date": {"type": "ask", "text": "Which day suits you? (for example 25/11/2026)", "validator": "date", "save_as": "attr:VisitDate",
                     "retry_text": "Please write the date like 25/11/2026.", "next": "time"},
            "time": {"type": "buttons", "text": "And what time?", "options": [
                {"id": "am", "title": "Morning", "next": "tag"}, {"id": "pm", "title": "Afternoon", "next": "tag"}]},
            "tag": {"type": "add_tag", "tag": "site-visit", "next": "done"},
            "done": {"type": "handoff", "text": "Noted for {{attr.VisitDate}}. Our engineer will confirm shortly.", "reason": "site visit requested"}}},
    },
    {
        "key": "quote_request", "title": "Collect details for a quote",
        "description": "Asks the project type, the area and the city, then passes the details to your team.",
        "trigger": {"type": "keyword", "match": "contains", "keywords": ["quote", "quotation", "price", "rate"]},
        "graph": {"entry": "type", "nodes": {
            "type": {"type": "list", "text": "What kind of work is it?", "button_label": "Choose one", "rows": [
                {"id": "boundary", "title": "Boundary wall", "next": "area"}, {"id": "house", "title": "House", "next": "area"},
                {"id": "commercial", "title": "Commercial", "next": "area"}, {"id": "other", "title": "Something else", "next": "area"}]},
            "area": {"type": "ask", "text": "Roughly how many square feet? (a number is fine)", "validator": "number", "save_as": "attr:AreaSqft",
                     "retry_text": "Please write just a number, like 1200.", "next": "city"},
            "city": {"type": "ask", "text": "Which city?", "save_as": "attr:City", "next": "tag"},
            "tag": {"type": "add_tag", "tag": "quote-requested", "next": "done"},
            "done": {"type": "handoff", "text": "Thank you. We will send you a quotation soon.", "reason": "quote requested"}}},
    },
    {
        "key": "feedback", "title": "Ask for feedback",
        "description": "Asks how it went; a poor rating is tagged and goes to a person straight away.",
        "trigger": {"type": "keyword", "match": "exact", "keywords": ["feedback"]},
        "graph": {"entry": "rate", "nodes": {
            "rate": {"type": "buttons", "text": "How was your experience with us?", "options": [
                {"id": "great", "title": "Great", "next": "thanks"}, {"id": "ok", "title": "It was OK", "next": "comment"},
                {"id": "bad", "title": "Not good", "next": "sorry"}]},
            "comment": {"type": "ask", "text": "Thank you. Anything we could do better?", "save_as": "var:comment", "next": "thanks"},
            "thanks": {"type": "send_text", "text": "Thank you for telling us!"},
            "sorry": {"type": "add_tag", "tag": "unhappy", "next": "person"},
            "person": {"type": "handoff", "text": "We are sorry. A manager will contact you today.", "reason": "unhappy customer"}}},
    },
    {
        "key": "after_hours", "title": "Reply when the office is closed",
        "description": "Any message that no other flow takes gets the right answer for the time of day.",
        "trigger": {"type": "default"},
        "graph": {"entry": "check", "nodes": {
            "check": {"type": "condition", "rules": [{"field": "hours", "op": "closed", "next": "closed"}], "else": "open"},
            "closed": {"type": "send_text", "text": "Thanks for your message. We are closed right now and will reply when we open."},
            "open": {"type": "handoff", "text": "Thanks for your message. A colleague will reply shortly.", "reason": "no flow matched"}}},
    },
]


# ── more, by topic ───────────────────────────────────────────────────────────────────────────────────────────────────────────────
# Every entry here passes `flows.validate` (a test enforces it). Entries marked needs_files send a picture or document: they carry a placeholder
# id, and Prism will not publish them until the owner chooses a real file from the Media Library. Entries marked needs_edit carry sample
# addresses or coordinates that must be replaced with the business's own.

NO_FILE = "00000000-0000-0000-0000-000000000000"
for _item in LIBRARY:
    _item.setdefault("topic", "Construction" if _item["key"] in ("lead_capture", "faq_menu", "site_visit", "quote_request") else "General")


def _flow(key, title, topic, description, trigger, entry, nodes, **flags):
    return {"key": key, "title": title, "topic": topic, "description": description, "trigger": trigger, "graph": {"entry": entry, "nodes": nodes}, **flags}


def _kw(*words, match="contains"):
    return {"type": "keyword", "match": match, "keywords": list(words)}


def _ask(text, save, nxt, validator="text", **more):
    return {"type": "ask", "text": text, "save_as": save, "validator": validator, "next": nxt, **more}


def _buttons(text, *options):
    return {"type": "buttons", "text": text, "options": [{"id": i, "title": t, "next": n} for i, t, n in options]}


def _rows(text, label, *rows):
    return {"type": "list", "text": text, "button_label": label, "rows": [{"id": i, "title": t, "next": n} for i, t, n in rows]}


def _hand(text, reason):
    return {"type": "handoff", "text": text, "reason": reason}


def _say(text, nxt=None):
    return {"type": "send_text", "text": text, **({"next": nxt} if nxt else {})}


LIBRARY += [
    # ── general ──────────────────────────────────────────────────────────────
    _flow("business_hours_router", "Answer by the day, the holiday and the hour", "General",
          "Any message no other flow takes: on a holiday it says you are closed, in working hours it hands to a person, otherwise it says when you reopen.",
          {"type": "default"}, "holiday", {
              "holiday": {"type": "condition", "rules": [{"field": "holiday", "op": "yes", "next": "closed_holiday"}], "else": "hours"},
              "hours": {"type": "condition", "rules": [{"field": "hours", "op": "open", "next": "open"}], "else": "closed_night"},
              "closed_holiday": _say("Thanks for your message. We are closed today for a holiday and will reply on the next working day."),
              "closed_night": _say("Thanks for your message. We are closed right now and will reply as soon as we open."),
              "open": _hand("Thanks for your message. A colleague will reply shortly.", "no flow matched")}),
    _flow("weekend_notice", "Tell weekend visitors you are closed", "General", "A short reply on Saturdays and Sundays, and a hand-over on weekdays.",
          {"type": "default"}, "day", {
              "day": {"type": "condition", "rules": [{"field": "weekday", "op": "in", "value": "sat,sun", "next": "weekend"}], "else": "weekday"},
              "weekend": _say("Thanks for writing. Our team is off at the weekend and will reply on Monday morning."),
              "weekday": _hand("Thanks for your message. A colleague will reply shortly.", "weekday message")}),
    _flow("festival_offer_window", "Share an offer only while it runs", "General", "Replies with the offer between two dates and a polite note after it ends.",
          _kw("offer", "discount"), "window", {
              "window": {"type": "condition", "rules": [{"field": "date", "op": "between", "value": "2026-11-01..2026-11-15", "next": "offer"}], "else": "over"},
              "offer": _buttons("Our festival offer is on until 15 November. Would you like the details?", ("yes", "Yes please", "talk"), ("no", "Not now", "bye")),
              "over": _say("That offer has ended. We will tell you about the next one."),
              "talk": _hand("Great. A colleague will share the details now.", "offer interest"),
              "bye": _say("No problem. Message us any time.")}, needs_edit=True),
    _flow("language_choice", "Let the customer choose a language", "General", "Asks English, Hindi or Gujarati once and saves the choice on the contact.",
          {"type": "first_message"}, "choose", {
              "choose": _buttons("Which language do you prefer?", ("en", "English", "en_set"), ("hi", "हिन्दी", "hi_set"), ("gu", "ગુજરાતી", "gu_set")),
              "en_set": {"type": "set_attribute", "key": "Language", "value": "English", "next": "go"},
              "hi_set": {"type": "set_attribute", "key": "Language", "value": "Hindi", "next": "go"},
              "gu_set": {"type": "set_attribute", "key": "Language", "value": "Gujarati", "next": "go"},
              "go": _hand("Thank you. A colleague will continue in your language.", "language chosen")}),
    _flow("lead_qualification", "Qualify a lead by budget and time", "General", "Two questions that sort a lead into hot, warm or cold and tag it.",
          _kw("interested", "enquiry", "inquiry"), "budget", {
              "budget": _rows("What is your budget?", "Choose one", ("low", "Under 1 lakh", "cold"), ("mid", "1 to 5 lakh", "when"), ("high", "Above 5 lakh", "when")),
              "when": _buttons("When do you want to start?", ("now", "This month", "hot"), ("soon", "In 2 to 3 months", "warm"), ("later", "Just exploring", "cold")),
              "hot": {"type": "add_tag", "tag": "hot-lead", "next": "person"},
              "warm": {"type": "add_tag", "tag": "warm-lead", "next": "thanks"},
              "cold": {"type": "add_tag", "tag": "cold-lead", "next": "thanks"},
              "thanks": _say("Thanks. We will keep you posted."),
              "person": _hand("Thanks. A colleague will call you today.", "hot lead")}),
    _flow("nps_survey", "Ask for a score from 0 to 10", "General", "A one-question survey; low scores go to a person.",
          _kw("survey", "rate us", match="contains"), "score", {
              "score": _ask("On a scale of 0 to 10, how likely are you to recommend us? Just send the number.", "attr:NpsScore", "check", "number", retry_text="Please send a number from 0 to 10."),
              "check": {"type": "condition", "rules": [{"field": "attr:NpsScore", "op": "lt", "value": "7", "next": "low"}], "else": "thanks"},
              "thanks": _say("Thank you for the score!"),
              "low": {"type": "add_tag", "tag": "detractor", "next": "person"},
              "person": _hand("We are sorry we fell short. A manager will contact you.", "low score")}),
    _flow("complaint_registration", "Register a complaint", "General", "Takes the details of a problem, tags it and hands it to a person.",
          _kw("complaint", "problem", "issue"), "what", {
              "what": _ask("I am sorry to hear that. Please describe the problem in a message.", "var:problem", "order"),
              "order": _ask("What is your order or invoice number? Write none if you do not have one.", "var:ref", "tag"),
              "tag": {"type": "add_tag", "tag": "complaint", "next": "done"},
              "done": _hand("Thank you. We have noted it and a manager will contact you today.", "complaint")}),
    _flow("appointment_booking", "Book an appointment", "General", "Asks the day and the time of day and hands over to confirm.",
          _kw("appointment", "book"), "date", {
              "date": _ask("Which day would you like? (for example 25/11/2026)", "attr:AppointmentDate", "slot", "date", retry_text="Please write the date like 25/11/2026."),
              "slot": _buttons("Which part of the day?", ("am", "Morning", "tag"), ("pm", "Afternoon", "tag"), ("eve", "Evening", "tag")),
              "tag": {"type": "add_tag", "tag": "appointment", "next": "done"},
              "done": _hand("Noted for {{attr.AppointmentDate}}. We will confirm the time shortly.", "appointment requested")}),
    _flow("share_location_pin", "Send the shop's location", "General", "When someone asks where you are, sends a map pin and the address.",
          _kw("location", "address", "where are you"), "pin", {
              "pin": {"type": "send_location", "latitude": 22.3072, "longitude": 73.1812, "name": "Our office", "address": "Change this to your address", "next": "ask"},
              "ask": _buttons("Would you like someone to call you?", ("yes", "Yes, call me", "person"), ("no", "No thanks", "bye")),
              "person": _hand("We will call you shortly.", "wants a call"),
              "bye": _say("Great. See you soon!")}, needs_edit=True),
    _flow("request_customer_location", "Ask the customer for their location", "General", "Asks them to share a pin so you can send the right team, then tags the chat.",
          _kw("service", "installation"), "ask", {
              "ask": {"type": "location_request", "text": "Please share your location so we can send the nearest team.", "timeout_minutes": 60, "next": "got", "timeout_next": "text"},
              "got": {"type": "add_tag", "tag": "location-received", "next": "thanks"},
              "text": _ask("We could not get a pin. Please type your area and city.", "attr:Area", "thanks"),
              "thanks": _hand("Thank you. A coordinator will confirm the visit.", "location shared")}),
    _flow("catalogue_on_request", "Send the catalogue", "General", "Sends your catalogue PDF when someone asks, then offers a call.",
          _kw("catalogue", "catalog", "brochure"), "file", {
              "file": {"type": "media", "kind": "document", "asset_id": NO_FILE, "caption": "Here is our catalogue.", "next": "ask"},
              "ask": _buttons("Would you like help choosing?", ("yes", "Yes please", "person"), ("no", "I will browse", "bye")),
              "person": _hand("A colleague will help you now.", "catalogue follow-up"),
              "bye": _say("Enjoy! Message us any time.")}, needs_files=True),
    _flow("lead_to_your_crm", "Pass a lead to your own system", "General", "Asks name and phone, posts them to your web address, then thanks the customer.",
          _kw("quote", "callback"), "name", {
              "name": _ask("What is your name?", "attr:FullName", "phone"),
              "phone": _ask("And a phone number we can call?", "var:phone", "post", "phone", retry_text="Please send a 10 digit phone number."),
              "post": {"type": "webhook", "url": "https://example.com/your-webhook", "method": "POST",
                       "body": "{\"name\": \"{{attr.FullName}}\", \"phone\": \"{{var.phone}}\", \"source\": \"whatsapp\"}", "headers": {"Content-Type": "application/json"}, "next": "done"},
              "done": _hand("Thank you {{attr.FullName}}. We will call you soon.", "lead captured")}, needs_edit=True),
    _flow("ad_lead_to_meta", "Tell Meta when an ad brings a lead", "General", "For people who arrive from a click-to-WhatsApp ad: takes their name, tells Meta it was a lead, hands over.",
          {"type": "first_message"}, "name", {
              "name": _ask("Welcome! What is your name?", "attr:FullName", "need"),
              "need": _ask("What are you looking for?", "var:need", "lead"),
              "lead": {"type": "capi_event", "event": "Lead", "next": "done"},
              "done": _hand("Thank you {{attr.FullName}}. A colleague will reply shortly.", "ad lead")}),
    _flow("order_status_lookup", "Look up an order's status", "General", "Asks for the order number and asks your own system for its status.",
          _kw("order status", "track", "where is my order"), "ask", {
              "ask": _ask("Please send your order number.", "var:order", "call"),
              "call": {"type": "api_request", "method": "GET", "url": "https://example.com/orders/{{var.order}}", "save": {"status": "status"}, "ok_next": "reply", "error_next": "sorry"},
              "reply": _say("Your order {{var.order}} is {{var.status}}."),
              "sorry": _hand("I could not find that order. A colleague will check for you.", "order lookup failed")}, needs_edit=True),
    # ── construction and trade ───────────────────────────────────────────────
    _flow("material_order_request", "Take a material order", "Construction", "Collects what material, how much and where to deliver, then hands over.",
          _kw("order", "material", "supply"), "what", {
              "what": _ask("What material do you need? (for example 10 mm TMT bars)", "var:material", "qty"),
              "qty": _ask("How much do you need? (a number and the unit)", "var:qty", "where"),
              "where": _ask("Where should it be delivered?", "attr:SiteAddress", "tag"),
              "tag": {"type": "add_tag", "tag": "material-order", "next": "done"},
              "done": _hand("Thank you. We will confirm the rate and the delivery date.", "material order")}),
    _flow("progress_update_request", "Send a site progress update", "Construction", "Customers type 'update' and get the latest progress; a coordinator adds photos.",
          _kw("update", "progress", match="contains"), "ask", {
              "ask": _ask("Which project is it about? Please write the site name or number.", "var:site", "tag"),
              "tag": {"type": "add_tag", "tag": "progress-request", "next": "done"},
              "done": _hand("Thanks. Our site engineer will send the latest progress for {{var.site}}.", "progress update requested")}),
    _flow("rate_card_share", "Share the rate card", "Construction", "Sends the current rate card and asks if a quotation is wanted.",
          _kw("rate card", "rates", "price list"), "file", {
              "file": {"type": "media", "kind": "document", "asset_id": NO_FILE, "caption": "Our current rate card.", "next": "ask"},
              "ask": _buttons("Shall we prepare a quotation for your project?", ("yes", "Yes", "quote"), ("no", "Not now", "bye")),
              "quote": _hand("Great. A colleague will take the details.", "rate card follow-up"),
              "bye": _say("No problem. The rate card is above for you to keep.")}, needs_files=True),
    # ── retail ───────────────────────────────────────────────────────────────
    _flow("product_enquiry", "Handle a product enquiry", "Retail", "Asks which product and the quantity, tags the chat and hands over.",
          _kw("price", "available", "stock"), "which", {
              "which": _ask("Which product are you asking about?", "var:product", "qty"),
              "qty": _ask("How many do you need?", "var:qty", "tag", "number", retry_text="Please send just a number."),
              "tag": {"type": "add_tag", "tag": "product-enquiry", "next": "done"},
              "done": _hand("Thanks. We will confirm the price and stock for {{var.product}}.", "product enquiry")}),
    _flow("returns_and_exchange", "Start a return or an exchange", "Retail", "Asks the order number and the reason and hands over.",
          _kw("return", "exchange", "refund"), "choice", {
              "choice": _buttons("What would you like to do?", ("return", "Return it", "order"), ("exchange", "Exchange it", "order"), ("refund", "Ask for a refund", "order")),
              "order": _ask("Please send your order number.", "var:order", "why"),
              "why": _ask("What went wrong?", "var:reason", "tag"),
              "tag": {"type": "add_tag", "tag": "return-request", "next": "done"},
              "done": _hand("Thanks. We have noted order {{var.order}} and will reply within a day.", "return requested")}),
    # ── education ────────────────────────────────────────────────────────────
    _flow("admission_enquiry", "Take an admission enquiry", "Education", "Asks the course, the student's name and a convenient time for a call.",
          _kw("admission", "course", "fees"), "course", {
              "course": _rows("Which course are you interested in?", "Choose a course", ("a", "Diploma", "name"), ("b", "Degree", "name"), ("c", "Short course", "name"), ("d", "Not sure yet", "name")),
              "name": _ask("What is the student's name?", "attr:StudentName", "time"),
              "time": _buttons("When can we call you?", ("am", "Morning", "tag"), ("pm", "Afternoon", "tag"), ("eve", "Evening", "tag")),
              "tag": {"type": "add_tag", "tag": "admission-enquiry", "next": "done"},
              "done": _hand("Thank you. Our counsellor will call you about {{attr.StudentName}}.", "admission enquiry")}),
    _flow("fee_payment_help", "Help with fee payment", "Education", "Answers common fee questions and hands over for anything else.",
          _kw("fee", "payment"), "menu", {
              "menu": _rows("How can we help with fees?", "Choose one", ("when", "When is it due?", "when_say"), ("how", "How do I pay?", "how_say"), ("other", "Something else", "person")),
              "when_say": _say("Fees are due by the 10th of every month.", "menu"),
              "how_say": _say("You can pay by UPI or at the office counter. Your admission number is your reference.", "menu"),
              "person": _hand("A colleague from accounts will help you.", "fee query")}, needs_edit=True),
    # ── healthcare ───────────────────────────────────────────────────────────
    _flow("clinic_appointment", "Book a clinic appointment", "Healthcare", "Asks who the patient is, what for and a day, then hands over to confirm a slot.",
          _kw("appointment", "doctor", "consult"), "who", {
              "who": _ask("What is the patient's name?", "attr:PatientName", "why"),
              "why": _rows("What is the visit for?", "Choose one", ("c", "Consultation", "date"), ("f", "Follow-up", "date"), ("t", "Test or report", "date")),
              "date": _ask("Which day? (for example 25/11/2026)", "attr:AppointmentDate", "tag", "date", retry_text="Please write the date like 25/11/2026."),
              "tag": {"type": "add_tag", "tag": "appointment", "next": "done"},
              "done": _hand("Thank you. We will confirm a time for {{attr.PatientName}} on {{attr.AppointmentDate}}.", "appointment requested")}),
    _flow("report_ready_follow_up", "Tell patients their report is ready", "Healthcare", "When a patient asks about a report, takes the ID and has the front desk reply.",
          _kw("report", "result"), "id", {
              "id": _ask("Please send the patient ID or the phone number used at the lab.", "var:patient", "tag"),
              "tag": {"type": "add_tag", "tag": "report-query", "next": "done"},
              "done": _hand("Thank you. The front desk will check the report for {{var.patient}}.", "report query")}),
    # ── real estate ──────────────────────────────────────────────────────────
    _flow("property_enquiry", "Take a property enquiry", "Real estate", "Asks buy or rent, the budget and the area, and hands over.",
          _kw("flat", "property", "house", "plot"), "what", {
              "what": _buttons("Are you looking to buy or to rent?", ("buy", "Buy", "budget"), ("rent", "Rent", "budget")),
              "budget": _rows("What is your budget?", "Choose one", ("a", "Under 30 lakh", "area"), ("b", "30 to 60 lakh", "area"), ("c", "60 lakh to 1 crore", "area"), ("d", "Above 1 crore", "area")),
              "area": _ask("Which area do you prefer?", "attr:PreferredArea", "tag"),
              "tag": {"type": "add_tag", "tag": "property-enquiry", "next": "done"},
              "done": _hand("Thank you. An advisor will share options in {{attr.PreferredArea}}.", "property enquiry")}),
    _flow("brochure_request", "Send a project brochure", "Real estate", "Sends the brochure and offers a site visit.",
          _kw("brochure", "details"), "file", {
              "file": {"type": "media", "kind": "document", "asset_id": NO_FILE, "caption": "Here is the project brochure.", "next": "visit"},
              "visit": _buttons("Would you like to see the site?", ("yes", "Book a visit", "person"), ("no", "Maybe later", "bye")),
              "person": _hand("Great. A colleague will fix a time with you.", "site visit interest"),
              "bye": _say("Okay. Message us whenever you are ready.")}, needs_files=True),
    # ── restaurant ───────────────────────────────────────────────────────────
    _flow("table_reservation", "Reserve a table", "Restaurant", "Asks how many people, the day and the time, then hands over to confirm.",
          _kw("table", "reservation", "book a table"), "people", {
              "people": _ask("How many people?", "var:people", "day", "number", retry_text="Please send a number."),
              "day": _ask("Which day? (for example 25/11/2026)", "var:day", "time", "date", retry_text="Please write the date like 25/11/2026."),
              "time": _buttons("Which time?", ("lunch", "Lunch", "tag"), ("early", "Early evening", "tag"), ("dinner", "Dinner", "tag")),
              "tag": {"type": "add_tag", "tag": "table-request", "next": "done"},
              "done": _hand("Thanks. We will confirm a table for {{var.people}} on {{var.day}}.", "table reservation")}),
    _flow("menu_share", "Share the menu", "Restaurant", "Sends the menu card and asks if they want to order.",
          _kw("menu"), "file", {
              "file": {"type": "media", "kind": "image", "asset_id": NO_FILE, "caption": "Our menu.", "next": "ask"},
              "ask": _buttons("Would you like to order?", ("yes", "Yes", "person"), ("no", "Just looking", "bye")),
              "person": _hand("Great. A colleague will take your order.", "wants to order"),
              "bye": _say("Enjoy browsing!")}, needs_files=True),
    # ── travel ───────────────────────────────────────────────────────────────
    _flow("trip_enquiry", "Take a trip enquiry", "Travel", "Asks where, when and how many travellers, then hands over.",
          _kw("trip", "tour", "holiday", "package"), "where", {
              "where": _ask("Where would you like to go?", "var:destination", "when"),
              "when": _ask("When are you planning to travel? (for example 20/12/2026)", "var:date", "who", "date", retry_text="Please write the date like 20/12/2026."),
              "who": _ask("How many travellers?", "var:travellers", "tag", "number", retry_text="Please send a number."),
              "tag": {"type": "add_tag", "tag": "trip-enquiry", "next": "done"},
              "done": _hand("Thank you. We will send options for {{var.destination}}.", "trip enquiry")}),
    # ── finance ──────────────────────────────────────────────────────────────
    _flow("loan_enquiry", "Take a loan enquiry", "Finance", "Asks the type of loan, the amount and the city and hands over to an advisor.",
          _kw("loan", "emi", "finance"), "type", {
              "type": _rows("Which loan do you need?", "Choose one", ("home", "Home loan", "amount"), ("biz", "Business loan", "amount"), ("car", "Vehicle loan", "amount"), ("pers", "Personal loan", "amount")),
              "amount": _ask("How much do you need? (a number)", "var:amount", "city", "number", retry_text="Please send just a number."),
              "city": _ask("Which city are you in?", "attr:City", "tag"),
              "tag": {"type": "add_tag", "tag": "loan-enquiry", "next": "done"},
              "done": _hand("Thank you. An advisor will call you about your {{var.amount}} loan.", "loan enquiry")}),
    # ── salon ────────────────────────────────────────────────────────────────
    _flow("salon_booking", "Book a salon visit", "Salon and wellness", "Asks the service and a day and hands over to confirm the time.",
          _kw("book", "appointment", "haircut", "facial"), "service", {
              "service": _rows("What would you like?", "Choose a service", ("hair", "Haircut or styling", "day"), ("skin", "Facial or skin care", "day"), ("spa", "Spa or massage", "day"), ("bridal", "Bridal package", "day")),
              "day": _ask("Which day suits you? (for example 25/11/2026)", "attr:VisitDate", "tag", "date", retry_text="Please write the date like 25/11/2026."),
              "tag": {"type": "add_tag", "tag": "salon-booking", "next": "done"},
              "done": _hand("Thanks. We will confirm a time on {{attr.VisitDate}}.", "salon booking")}),
    # ── automotive ───────────────────────────────────────────────────────────
    _flow("vehicle_service_booking", "Book a vehicle service", "Automotive", "Asks the model, the registration number and a day and hands over.",
          _kw("service", "servicing"), "model", {
              "model": _ask("Which vehicle is it? (make and model)", "var:model", "reg"),
              "reg": _ask("What is the registration number?", "var:reg", "day"),
              "day": _ask("Which day would you like to bring it? (for example 25/11/2026)", "var:day", "tag", "date", retry_text="Please write the date like 25/11/2026."),
              "tag": {"type": "add_tag", "tag": "service-booking", "next": "done"},
              "done": _hand("Thanks. We will confirm the slot for {{var.reg}} on {{var.day}}.", "service booking")}),
    # ── events ───────────────────────────────────────────────────────────────
    _flow("event_registration", "Register for an event", "Events", "Takes a name, an email and the number of seats, tags the person and confirms.",
          _kw("register", "event", "ticket"), "name", {
              "name": _ask("What is your name?", "attr:FullName", "email"),
              "email": _ask("And your email address?", "attr:Email", "seats", "email", retry_text="Please send a valid email address."),
              "seats": _ask("How many seats?", "var:seats", "tag", "number", retry_text="Please send a number."),
              "tag": {"type": "add_tag", "tag": "event-registered", "next": "done"},
              "done": _hand("You are registered, {{attr.FullName}}. We will send your pass by email.", "event registration")}),
]


def get(key: str):
    return next((item for item in LIBRARY if item["key"] == key), None)
