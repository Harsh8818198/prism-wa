"""Ready-to-submit templates (spec WS4 "Library").

Written fresh for Prism's buyers: the first seven are for a construction
business (quotations, site visits, stage payments, RA bills, dispatches, BOQ),
then the general ones every business sends, then more than a hundred by industry. Each passes `templates.lint`
with no errors and no warnings (a test enforces it), so submitting one is a
matter of choosing a name.

English first. Hindi and Gujarati text goes beside these, not instead of them.
Categories are our best reading of Meta's definitions; Meta can recategorise a
template when it reviews it, and `template_category` webhooks keep our copy honest.
"""
from __future__ import annotations


def _body(text: str, *samples: str) -> dict:
    return {"type": "BODY", "text": text, "example": {"body_text": [list(samples)]}}


LIBRARY = [
    # ── construction ─────────────────────────────────────────────────────────
    {"key": "quotation_sent", "title": "Quotation sent", "category": "UTILITY", "language": "en_US",
     "components": [
         _body("Hello {{1}}, thank you for your enquiry. We have prepared quotation {{2}} for your project and "
               "sent it to your email. Please reply here if you would like to go through it together.",
               "Asha", "Q-2041"),
         {"type": "FOOTER", "text": "Reply to this message anytime"}]},
    {"key": "quotation_followup", "title": "Quotation follow-up", "category": "MARKETING", "language": "en_US",
     "components": [
         _body("Hello {{1}}, a quick follow-up on quotation {{2}} that we shared with you. If you have any "
               "questions on the rates or the scope, we are happy to help.", "Asha", "Q-2041"),
         {"type": "BUTTONS", "buttons": [{"type": "QUICK_REPLY", "text": "Call me back"},
                                         {"type": "QUICK_REPLY", "text": "Not now"}]}]},
    {"key": "site_visit_confirmation", "title": "Site visit confirmation", "category": "UTILITY", "language": "en_US",
     "components": [
         _body("Hello {{1}}, your site visit is confirmed for {{2}}. Our engineer will meet you at the project "
               "location. Please keep the drawings ready if you have them.", "Asha", "Saturday 10 AM"),
         {"type": "BUTTONS", "buttons": [{"type": "QUICK_REPLY", "text": "Confirm"},
                                         {"type": "QUICK_REPLY", "text": "Reschedule"}]}]},
    {"key": "milestone_payment_reminder", "title": "Milestone payment reminder", "category": "UTILITY",
     "language": "en_US",
     "components": [
         _body("Hello {{1}}, this is a reminder that the {{2}} milestone payment of {{3}} for your project is "
               "now due. Please let us know once the payment has been made.", "Asha", "plinth level", "Rs 2,50,000")]},
    {"key": "ra_bill_submitted", "title": "Running account bill submitted", "category": "UTILITY", "language": "en_US",
     "components": [
         _body("Hello {{1}}, running account bill number {{2}} for work completed up to {{3}} has been submitted "
               "for your approval. Please review it and confirm.", "Asha", "RA-03", "15 October")]},
    {"key": "material_dispatch_update", "title": "Material dispatch update", "category": "UTILITY", "language": "en_US",
     "components": [
         _body("Hello {{1}}, your material order {{2}} has been dispatched and should reach your site by {{3}}. "
               "Please make sure someone is available to receive it.", "Asha", "ORD-884", "tomorrow evening")]},
    {"key": "boq_ready", "title": "BOQ ready", "category": "UTILITY", "language": "en_US",
     "components": [
         {"type": "HEADER", "format": "TEXT", "text": "Your BOQ is ready"},
         _body("Hello {{1}}, the bill of quantities for {{2}} is ready. We have sent it to your email. Reply here "
               "if you would like us to walk you through the items.", "Asha", "the boundary wall project")]},
    # ── general ──────────────────────────────────────────────────────────────
    {"key": "welcome", "title": "Welcome", "category": "UTILITY", "language": "en_US",
     "components": [
         _body("Hello {{1}}, welcome and thank you for contacting us. A member of our team will reply shortly. "
               "You can also send your question here at any time.", "Asha")]},
    {"key": "order_confirmation", "title": "Order confirmation", "category": "UTILITY", "language": "en_US",
     "components": [
         _body("Hello {{1}}, we have received your order {{2}} and it is being prepared. We will message you "
               "again as soon as it is on its way.", "Asha", "ORD-1042")]},
    {"key": "appointment_reminder", "title": "Appointment reminder", "category": "UTILITY", "language": "en_US",
     "components": [
         _body("Hello {{1}}, this is a reminder of your appointment on {{2}}. Reply to this message if you need "
               "to change the time.", "Asha", "Friday at 4 PM")]},
    {"key": "payment_reminder", "title": "Payment reminder", "category": "UTILITY", "language": "en_US",
     "components": [
         _body("Hello {{1}}, a friendly reminder that invoice {{2}} for {{3}} is due on {{4}}. If you have already "
               "paid, please ignore this message.", "Asha", "INV-330", "Rs 18,000", "20 October")]},
    {"key": "feedback_request", "title": "Feedback request", "category": "MARKETING", "language": "en_US",
     "components": [
         _body("Hello {{1}}, thank you for choosing us. We would love to hear how we did. Reply with a number "
               "from one to five and any comments you would like to share.", "Asha")]},
    {"key": "festival_greeting", "title": "Festival greeting", "category": "MARKETING", "language": "en_US",
     "components": [
         _body("Hello {{1}}, wishing you and your family a very happy {{2}} from all of us. Thank you for being "
               "with us this year.", "Asha", "Diwali"),
         {"type": "BUTTONS", "buttons": [{"type": "QUICK_REPLY", "text": "Stop messages"}]}]},
    {"key": "otp_copy_code", "title": "One-time password (copy code)", "category": "AUTHENTICATION",
     "language": "en_US",
     "components": [
         {"type": "BODY", "add_security_recommendation": True},
         {"type": "FOOTER", "code_expiration_minutes": 10},
         {"type": "BUTTONS", "buttons": [{"type": "OTP", "otp_type": "COPY_CODE", "text": "Copy code"}]}]},
]


def get(key: str) -> dict:
    for item in LIBRARY:
        if item["key"] == key:
            return item
    raise KeyError(key)


# ── more, by industry (spec WS4 asks for a library of 100+) ───────────────────────────────────────────────────────────────────────
# Same rules as above: each passes `templates.lint` with no errors and no warnings, and a test enforces it. Entries that need a
# picture, video or document in the header carry `needs_media`: the editor asks for the sample file when one is chosen.

def _btn(spec) -> dict:
    kind, text = spec[0], spec[1]
    if kind == "qr":
        return {"type": "QUICK_REPLY", "text": text}
    if kind == "url":
        out = {"type": "URL", "text": text, "url": spec[2]}
        if "{{1}}" in spec[2]:
            out["example"] = [spec[2].replace("{{1}}", "sample")]
        return out
    if kind == "phone":
        return {"type": "PHONE_NUMBER", "text": text, "phone_number": spec[2]}
    raise ValueError(kind)


def _t(key, title, industry, category, text, samples=(), *, footer="", buttons=(), header="", header_media="", lang="en_US"):
    comps = []
    if header_media:
        comps.append({"type": "HEADER", "format": header_media})
    elif header:
        comps.append({"type": "HEADER", "format": "TEXT", "text": header})
    comps.append(_body(text, *samples) if samples else {"type": "BODY", "text": text})
    if footer:
        comps.append({"type": "FOOTER", "text": footer})
    if buttons:
        comps.append({"type": "BUTTONS", "buttons": [_btn(b) for b in buttons]})
    item = {"key": key, "title": title, "industry": industry, "category": category, "language": lang, "components": comps}
    if header_media:
        item["needs_media"] = header_media.lower()
    return item


U, M, A = "UTILITY", "MARKETING", "AUTHENTICATION"
STOP = ("qr", "Stop messages")

_MORE = [
    # ── construction ─────────────────────────────────────────────────────────
    _t("site_progress_update", "Site progress update", "Construction", U,
       "Hello {{1}}, here is the progress update for {{2}}: about {{3}} of the planned work is complete. Our site "
       "engineer can share photos if you would like to see them.", ("Asha", "the boundary wall", "60%")),
    _t("payment_received_project", "Payment received for a project", "Construction", U,
       "Hello {{1}}, we have received your payment of {{2}} against {{3}}. Thank you. The receipt will reach your "
       "email shortly.", ("Asha", "Rs 2,50,000", "the plinth stage")),
    _t("drawing_approval_request", "Drawing approval request", "Construction", U,
       "Hello {{1}}, the revised drawing {{2}} is ready for your approval. Please review it and tell us whether we "
       "may proceed with the work.", ("Asha", "A-104 rev C"),
       buttons=(("qr", "Approved"), ("qr", "Need changes"))),
    _t("handover_schedule", "Handover schedule", "Construction", U,
       "Hello {{1}}, the handover of {{2}} is planned for {{3}}. Please bring a person from your side who can sign "
       "the completion papers with us.", ("Asha", "your workshop shed", "30 November")),
    _t("site_safety_notice", "Site safety notice", "Construction", U,
       "Hello {{1}}, there will be heavy lifting work at your site on {{2}}. Please keep the area clear of people "
       "and vehicles between the working hours we have shared.", ("Asha", "Tuesday")),
    _t("material_shortage_notice", "Material delay notice", "Construction", U,
       "Hello {{1}}, the delivery of {{2}} for your site is delayed by {{3}} because of a supplier problem. We are "
       "sorry and will confirm the new date as soon as we have it.", ("Asha", "steel bars", "two days")),
    _t("completion_certificate_ready", "Completion certificate ready", "Construction", U,
       "Hello {{1}}, the completion certificate for {{2}} has been prepared and sent to your email. Please keep a "
       "copy for your records.", ("Asha", "the warehouse extension")),
    _t("new_project_enquiry_ack", "New enquiry received", "Construction", U,
       "Hello {{1}}, thank you for sharing your requirement for {{2}}. Our estimator will call you within one "
       "working day to understand the details.", ("Asha", "a compound wall"),
       buttons=(("qr", "Call me now"), ("qr", "Call tomorrow"))),
    _t("site_location_pin", "Site location", "Construction", U,
       "Hello {{1}}, our engineer is on the way to your site for {{2}}. Please keep the location pin shared with "
       "you handy in case the road is hard to find.", ("Asha", "the measurement visit"), header_media="LOCATION"),
    # ── retail and e-commerce ────────────────────────────────────────────────
    _t("order_shipped", "Order shipped", "Retail and e-commerce", U,
       "Hello {{1}}, your order {{2}} has been shipped with {{3}}. You can track it with the button below.",
       ("Asha", "ORD-1042", "Delhivery"), buttons=(("url", "Track order", "https://example.com/track/{{1}}"),)),
    _t("out_for_delivery", "Out for delivery", "Retail and e-commerce", U,
       "Hello {{1}}, your order {{2}} is out for delivery today. Please keep your phone close so the delivery "
       "partner can reach you.", ("Asha", "ORD-1042")),
    _t("order_delivered_feedback", "Delivered, how was it?", "Retail and e-commerce", M,
       "Hello {{1}}, your order {{2}} has been delivered. We hope you like it. Tell us how the delivery went by "
       "choosing an option below.", ("Asha", "ORD-1042"), buttons=(("qr", "Great"), ("qr", "Could be better"))),
    _t("abandoned_cart", "Items left in the cart", "Retail and e-commerce", M,
       "Hello {{1}}, you left {{2}} in your cart. They are still available, and checking out takes just a minute.",
       ("Asha", "two items"), buttons=(("url", "Complete my order", "https://example.com/cart/{{1}}"), STOP)),
    _t("back_in_stock", "Back in stock", "Retail and e-commerce", M,
       "Hello {{1}}, the {{2}} you asked about is back in stock. Stocks move fast, so order early if you want it.",
       ("Asha", "steel almirah"), buttons=(("qr", "Show me"), STOP)),
    _t("return_request_received", "Return request received", "Retail and e-commerce", U,
       "Hello {{1}}, we have received your return request for order {{2}}. Our partner will collect the item within "
       "{{3}}.", ("Asha", "ORD-1042", "three working days")),
    _t("refund_processed", "Refund processed", "Retail and e-commerce", U,
       "Hello {{1}}, the refund of {{2}} for order {{3}} has been processed. It should appear in your account "
       "within a few working days.", ("Asha", "Rs 1,499", "ORD-1042")),
    _t("review_request", "Please review your purchase", "Retail and e-commerce", M,
       "Hello {{1}}, thank you for buying {{2}}. A short review from you helps other buyers decide, and it takes "
       "less than a minute.", ("Asha", "the work desk"), buttons=(("url", "Write a review", "https://example.com/review/{{1}}"),)),
    _t("flash_sale", "Flash sale", "Retail and e-commerce", M,
       "Hello {{1}}, our flash sale starts today and runs until {{2}}. Take a look at what is on sale before the "
       "stock runs out.", ("Asha", "Sunday night"), buttons=(("url", "See the sale", "https://example.com/sale"), STOP)),
    _t("loyalty_points_update", "Loyalty points update", "Retail and e-commerce", U,
       "Hello {{1}}, you now have {{2}} loyalty points in your account. You can use them on your next order.",
       ("Asha", "240")),
    _t("wishlist_price_drop", "Price drop on a wishlist item", "Retail and e-commerce", M,
       "Hello {{1}}, the price of {{2}} on your wishlist has come down to {{3}}. Order now if you were waiting for "
       "this.", ("Asha", "the office chair", "Rs 5,999"), buttons=(("url", "View item", "https://example.com/item/{{1}}"), STOP)),
    # ── education ────────────────────────────────────────────────────────────
    _t("admission_enquiry_ack", "Admission enquiry received", "Education", U,
       "Hello {{1}}, thank you for your interest in {{2}}. Our admissions team will get in touch with you within "
       "one working day.", ("Asha", "the diploma in civil engineering")),
    _t("fee_reminder", "Fee reminder", "Education", U,
       "Hello {{1}}, this is a reminder that the fee of {{2}} for {{3}} is due on {{4}}. Please ignore this "
       "message if you have already paid.", ("Asha", "Rs 12,000", "the second term", "10 November")),
    _t("exam_schedule", "Exam schedule", "Education", U,
       "Hello {{1}}, the {{2}} exams begin on {{3}}. The full timetable has been shared on the notice board and "
       "your email.", ("Asha", "first term", "5 December")),
    _t("result_declared", "Result declared", "Education", U,
       "Hello {{1}}, the result for {{2}} has been declared. You can see your marks on the student portal with "
       "your roll number.", ("Asha", "the first term exams")),
    _t("parent_meeting", "Parent meeting", "Education", U,
       "Hello {{1}}, a parent and teacher meeting for {{2}} is planned on {{3}}. Please confirm whether you can "
       "attend.", ("Asha", "class 8", "Saturday at 11 AM"), buttons=(("qr", "I will attend"), ("qr", "Cannot attend"))),
    _t("class_cancelled", "Class cancelled", "Education", U,
       "Hello {{1}}, today's {{2}} class has been cancelled because the teacher is unwell. We will share the new "
       "date soon.", ("Asha", "mathematics")),
    _t("demo_class_invite", "Free demo class invite", "Education", M,
       "Hello {{1}}, we are running a demo class for {{2}} on {{3}}. Come and see how our teaching works before "
       "you decide.", ("Asha", "the engineering entrance course", "Sunday at 10 AM"),
       buttons=(("qr", "Reserve a seat"), STOP)),
    _t("course_certificate_ready", "Course certificate ready", "Education", U,
       "Hello {{1}}, congratulations on completing {{2}}. Your certificate is ready and has been sent to your "
       "email.", ("Asha", "the AutoCAD course")),
    # ── healthcare and clinics ───────────────────────────────────────────────
    _t("appointment_confirmation_clinic", "Appointment confirmed", "Healthcare and clinics", U,
       "Hello {{1}}, your appointment with {{2}} is confirmed for {{3}}. Please arrive ten minutes early and "
       "carry your previous reports.", ("Asha", "Dr Mehta", "Friday at 4 PM"),
       buttons=(("qr", "Confirm"), ("qr", "Reschedule"))),
    _t("appointment_reminder_clinic", "Appointment reminder", "Healthcare and clinics", U,
       "Hello {{1}}, this is a reminder of your appointment with {{2}} tomorrow at {{3}}. Reply to this message "
       "if you need to change it.", ("Asha", "Dr Mehta", "4 PM")),
    _t("lab_report_ready", "Lab report ready", "Healthcare and clinics", U,
       "Hello {{1}}, your {{2}} report is ready. You can collect it from the front desk or download it from the "
       "link below.", ("Asha", "blood test"), buttons=(("url", "Download report", "https://example.com/reports/{{1}}"),)),
    _t("prescription_refill", "Prescription refill reminder", "Healthcare and clinics", U,
       "Hello {{1}}, your prescription for {{2}} is due for a refill on {{3}}. Reply to this message and our "
       "pharmacy will keep it ready for you.", ("Asha", "your regular medicines", "20 November")),
    _t("vaccination_reminder", "Vaccination reminder", "Healthcare and clinics", U,
       "Hello {{1}}, {{2}} is due for the {{3}} vaccination on {{4}}. Please bring the vaccination card with you.",
       ("Asha", "your child", "second dose", "12 November")),
    _t("follow_up_checkup", "Follow-up check-up", "Healthcare and clinics", U,
       "Hello {{1}}, it is time for your follow-up check-up after {{2}}. Reply here and we will find a convenient "
       "slot for you.", ("Asha", "your treatment")),
    _t("health_camp_invite", "Health camp invitation", "Healthcare and clinics", M,
       "Hello {{1}}, we are holding a health check-up camp at {{2}} on {{3}}. Come with your family and get a basic "
       "check-up from our doctors.", ("Asha", "our main clinic", "Sunday morning"),
       buttons=(("qr", "Register me"), STOP)),
    _t("doctor_running_late", "Doctor running late", "Healthcare and clinics", U,
       "Hello {{1}}, {{2}} is running about {{3}} behind schedule today. We are sorry for the wait and will see you "
       "as soon as possible.", ("Asha", "Dr Mehta", "twenty minutes")),
    # ── real estate ──────────────────────────────────────────────────────────
    _t("site_visit_invite", "Site visit invitation", "Real estate", M,
       "Hello {{1}}, you can see {{2}} in person this weekend. Choose a time that suits you and our team will take "
       "you around.", ("Asha", "our new apartments"), buttons=(("qr", "Saturday"), ("qr", "Sunday"), STOP)),
    _t("booking_confirmation_property", "Booking confirmed", "Real estate", U,
       "Hello {{1}}, your booking of unit {{2}} in {{3}} is confirmed. Our team will share the agreement and the "
       "payment schedule with you shortly.", ("Asha", "B-304", "Shreeji Heights")),
    _t("payment_due_property", "Instalment due", "Real estate", U,
       "Hello {{1}}, the instalment of {{2}} for unit {{3}} is due on {{4}}. Please share the payment confirmation "
       "here once it is done.", ("Asha", "Rs 4,00,000", "B-304", "15 November")),
    _t("possession_update", "Possession update", "Real estate", U,
       "Hello {{1}}, the possession of unit {{2}} is expected by {{3}}. We will inform you about the final "
       "inspection once the building is ready.", ("Asha", "B-304", "March")),
    _t("new_launch", "New project launch", "Real estate", M,
       "Hello {{1}}, we are launching {{2}} in {{3}}. Request the brochure and the price list to see whether it "
       "suits you.", ("Asha", "Shreeji Residency", "Vadodara"), buttons=(("qr", "Send brochure"), STOP)),
    _t("registration_reminder", "Registration appointment", "Real estate", U,
       "Hello {{1}}, the registration of your property is scheduled for {{2}} at the sub-registrar office. Please "
       "carry the original documents we listed for you.", ("Asha", "Monday at 11 AM")),
    _t("loan_assistance", "Home loan assistance", "Real estate", M,
       "Hello {{1}}, we have tied up with banks that can help you with a home loan for {{2}}. Reply if you would "
       "like our loan advisor to call you.", ("Asha", "your new flat"), buttons=(("qr", "Call me"), STOP)),
    _t("brochure_share", "Project brochure", "Real estate", M,
       "Hello {{1}}, here is the brochure for {{2}} that you asked about. Let us know if you would like to visit "
       "the site.", ("Asha", "Shreeji Heights"), header_media="DOCUMENT", buttons=(("qr", "Plan a visit"), STOP)),
    # ── restaurants and food ─────────────────────────────────────────────────
    _t("table_booking_confirmation", "Table booking confirmed", "Restaurants and food", U,
       "Hello {{1}}, your table for {{2}} is booked for {{3}}. Please let us know if your plans change.",
       ("Asha", "four people", "Saturday at 8 PM"), buttons=(("qr", "Confirm"), ("qr", "Cancel booking"))),
    _t("food_order_received", "Order received", "Restaurants and food", U,
       "Hello {{1}}, we have received your order {{2}} and the kitchen has started on it. It should be ready in "
       "about {{3}}.", ("Asha", "F-218", "30 minutes")),
    _t("order_ready_pickup", "Order ready for pickup", "Restaurants and food", U,
       "Hello {{1}}, your order {{2}} is ready for pickup at our counter. Please show this message when you "
       "arrive.", ("Asha", "F-218")),
    _t("delivery_eta", "Delivery on the way", "Restaurants and food", U,
       "Hello {{1}}, your food is on the way with {{2}} and should reach you in about {{3}}.",
       ("Asha", "our delivery partner Ravi", "15 minutes")),
    _t("food_feedback", "How was the food?", "Restaurants and food", M,
       "Hello {{1}}, thank you for ordering from us. How was your meal? Your feedback helps our kitchen do better.",
       ("Asha",), buttons=(("qr", "Loved it"), ("qr", "It was okay"), ("qr", "Not happy"))),
    _t("weekend_special", "Weekend special", "Restaurants and food", M,
       "Hello {{1}}, this weekend our chef is serving {{2}} as a special. Reserve your table early because seats "
       "fill up fast.", ("Asha", "a Kathiyawadi thali"), buttons=(("qr", "Reserve a table"), STOP)),
    _t("dining_reward", "Dining reward", "Restaurants and food", U,
       "Hello {{1}}, you have earned a reward on your last {{2}} visits to us. Show this message at the counter to "
       "use it on your next meal.", ("Asha", "five")),
    # ── travel and hospitality ───────────────────────────────────────────────
    _t("hotel_booking_confirmed", "Hotel booking confirmed", "Travel and hospitality", U,
       "Hello {{1}}, your stay at {{2}} from {{3}} to {{4}} is confirmed. We look forward to hosting you.",
       ("Asha", "Hotel Surya", "12 November", "14 November")),
    _t("check_in_information", "Check-in information", "Travel and hospitality", U,
       "Hello {{1}}, check-in at {{2}} begins at {{3}}. Please carry a government photo identity for every guest.",
       ("Asha", "Hotel Surya", "2 PM")),
    _t("itinerary_ready", "Itinerary ready", "Travel and hospitality", U,
       "Hello {{1}}, the itinerary for your trip to {{2}} is ready and has been sent to your email. Please check "
       "it and tell us if anything needs to change.", ("Asha", "Udaipur")),
    _t("flight_delay", "Flight delay", "Travel and hospitality", U,
       "Hello {{1}}, your flight {{2}} is delayed and is now expected to leave at {{3}}. We are sorry for the "
       "trouble and will keep you updated.", ("Asha", "6E 214", "9:40 PM")),
    _t("pickup_details", "Pickup details", "Travel and hospitality", U,
       "Hello {{1}}, your driver {{2}} will pick you up at {{3}}. You can call him if you have trouble finding the "
       "pickup point.", ("Asha", "Ramesh", "6 AM tomorrow"), buttons=(("phone", "Call driver", "+919876543210"),)),
    _t("checkout_feedback", "Stay feedback", "Travel and hospitality", M,
       "Hello {{1}}, thank you for staying with us at {{2}}. We would love to know how your stay was, and it takes "
       "just a minute to share.", ("Asha", "Hotel Surya"), buttons=(("qr", "Excellent"), ("qr", "Good"), ("qr", "Needs work"))),
    _t("holiday_package", "Holiday package", "Travel and hospitality", M,
       "Hello {{1}}, our {{2}} holiday package for {{3}} is open for booking. Reply to get the plan and the dates "
       "that are available.", ("Asha", "Kerala", "the winter break"), buttons=(("qr", "Send details"), STOP)),
    _t("travel_documents_needed", "Travel documents needed", "Travel and hospitality", U,
       "Hello {{1}}, to complete your booking for {{2}} we need a copy of the passport for every traveller. Please "
       "send them here by {{3}}.", ("Asha", "Dubai", "Friday")),
    # ── finance and insurance ────────────────────────────────────────────────
    _t("emi_reminder", "EMI reminder", "Finance and insurance", U,
       "Hello {{1}}, your EMI of {{2}} for loan account {{3}} is due on {{4}}. Please keep enough balance in your "
       "account.", ("Asha", "Rs 8,450", "LN-5521", "5 November")),
    _t("payment_received_finance", "Payment received", "Finance and insurance", U,
       "Hello {{1}}, we have received your payment of {{2}} towards account {{3}}. Thank you for paying on time.",
       ("Asha", "Rs 8,450", "LN-5521")),
    _t("policy_renewal", "Policy renewal", "Finance and insurance", U,
       "Hello {{1}}, your {{2}} policy {{3}} expires on {{4}}. Renew before that date so your cover does not "
       "lapse.", ("Asha", "vehicle insurance", "POL-9012", "30 November"),
       buttons=(("qr", "Renew now"), ("qr", "Call me"))),
    _t("premium_due", "Premium due", "Finance and insurance", U,
       "Hello {{1}}, the premium of {{2}} for policy {{3}} is due on {{4}}. Pay on time to keep your benefits "
       "active.", ("Asha", "Rs 12,000", "POL-9012", "18 November")),
    _t("kyc_pending", "KYC pending", "Finance and insurance", U,
       "Hello {{1}}, your KYC for account {{2}} is still pending. Please send the documents we listed so that "
       "your account stays active.", ("Asha", "AC-7781")),
    _t("statement_ready", "Statement ready", "Finance and insurance", U,
       "Hello {{1}}, your statement for {{2}} is ready. We have sent it to your registered email address.",
       ("Asha", "October")),
    _t("claim_status", "Claim status", "Finance and insurance", U,
       "Hello {{1}}, the status of your claim {{2}} is now {{3}}. We will message you again at the next step.",
       ("Asha", "CLM-3340", "under review")),
    _t("loan_approved", "Loan approved", "Finance and insurance", U,
       "Hello {{1}}, your loan application {{2}} has been approved. Our officer will call you to complete the "
       "remaining formalities.", ("Asha", "APP-2231")),
    _t("document_upload_request", "Documents needed", "Finance and insurance", U,
       "Hello {{1}}, we need {{2}} to move your application {{3}} forward. Please send clear photos of them here.",
       ("Asha", "your latest bank statement", "APP-2231")),
    # ── salon and wellness ───────────────────────────────────────────────────
    _t("salon_appointment_confirm", "Appointment confirmed", "Salon and wellness", U,
       "Hello {{1}}, your {{2}} appointment is confirmed for {{3}}. We look forward to seeing you.",
       ("Asha", "haircut and styling", "Saturday at 5 PM"), buttons=(("qr", "Confirm"), ("qr", "Reschedule"))),
    _t("salon_reminder", "Appointment reminder", "Salon and wellness", U,
       "Hello {{1}}, a reminder of your {{2}} appointment tomorrow at {{3}}. Please tell us if you cannot make it.",
       ("Asha", "facial", "5 PM")),
    _t("package_expiry", "Package expiring", "Salon and wellness", U,
       "Hello {{1}}, your {{2}} package has {{3}} sessions left and expires on {{4}}. Book your remaining sessions "
       "before then.", ("Asha", "spa", "two", "30 November")),
    _t("birthday_treat", "Birthday treat", "Salon and wellness", M,
       "Hello {{1}}, happy birthday from all of us. We have a small treat waiting for you this month, so visit us "
       "and show this message.", ("Asha",), buttons=(("qr", "Book a visit"), STOP)),
    _t("membership_renewal", "Membership renewal", "Salon and wellness", U,
       "Hello {{1}}, your membership ends on {{2}}. Renew now to keep your benefits and your preferred slots.",
       ("Asha", "30 November"), buttons=(("qr", "Renew"), ("qr", "Not now"))),
    _t("salon_feedback", "Visit feedback", "Salon and wellness", M,
       "Hello {{1}}, thank you for visiting us. Tell us how we did so that we can keep improving.", ("Asha",),
       buttons=(("qr", "Loved it"), ("qr", "Could improve"))),
    # ── automobile ───────────────────────────────────────────────────────────
    _t("service_due", "Service due", "Automobile", U,
       "Hello {{1}}, your {{2}} is due for its regular service on or before {{3}}. Reply to book a convenient "
       "slot at our workshop.", ("Asha", "Honda City", "15 November"), buttons=(("qr", "Book service"), ("qr", "Remind me later"))),
    _t("service_booked", "Service booked", "Automobile", U,
       "Hello {{1}}, your service for {{2}} is booked on {{3}}. Please bring the vehicle and its service book to "
       "the workshop.", ("Asha", "GJ-06 AB 1234", "Monday at 10 AM")),
    _t("vehicle_ready", "Vehicle ready", "Automobile", U,
       "Hello {{1}}, your vehicle {{2}} is ready for delivery. The final bill is {{3}}. You can collect it any "
       "time before our workshop closes.", ("Asha", "GJ-06 AB 1234", "Rs 6,200")),
    _t("insurance_expiry_vehicle", "Vehicle insurance expiring", "Automobile", U,
       "Hello {{1}}, the insurance of your vehicle {{2}} expires on {{3}}. Reply if you would like us to arrange "
       "the renewal.", ("Asha", "GJ-06 AB 1234", "28 November")),
    _t("test_drive_confirmation", "Test drive confirmed", "Automobile", U,
       "Hello {{1}}, your test drive of the {{2}} is confirmed for {{3}}. Please carry your driving licence.",
       ("Asha", "Tata Nexon", "Saturday at 11 AM")),
    _t("new_model_launch", "New model launch", "Automobile", M,
       "Hello {{1}}, the new {{2}} has arrived at our showroom. Come and see it, and take a test drive if you "
       "like.", ("Asha", "SUV range"), buttons=(("qr", "Book a test drive"), STOP)),
    # ── events ───────────────────────────────────────────────────────────────
    _t("event_registration_confirm", "Registration confirmed", "Events", U,
       "Hello {{1}}, you are registered for {{2}} on {{3}}. We will send your entry pass a day before the event.",
       ("Asha", "the construction expo", "12 December")),
    _t("event_reminder", "Event reminder", "Events", U,
       "Hello {{1}}, {{2}} starts tomorrow at {{3}}. Please arrive early to avoid the queue at the entrance.",
       ("Asha", "the construction expo", "10 AM")),
    _t("event_pass_ready", "Entry pass ready", "Events", U,
       "Hello {{1}}, your entry pass for {{2}} is ready. Show it at the gate on the day.", ("Asha", "the construction expo"),
       header_media="DOCUMENT"),
    _t("event_feedback", "Event feedback", "Events", M,
       "Hello {{1}}, thank you for joining {{2}}. We would value your thoughts on what we can do better next time.",
       ("Asha", "the construction expo"), buttons=(("qr", "Great event"), ("qr", "Needs work"))),
    _t("early_bird_tickets", "Early bird tickets", "Events", M,
       "Hello {{1}}, early registration for {{2}} is now open and seats are limited. Reserve yours before the "
       "early period closes.", ("Asha", "the annual builders meet"), buttons=(("qr", "Register me"), STOP)),
    # ── general ──────────────────────────────────────────────────────────────
    _t("payment_received_generic", "Payment received", "General", U,
       "Hello {{1}}, we have received your payment of {{2}} for {{3}}. Thank you. A receipt will be sent to your "
       "email.", ("Asha", "Rs 18,000", "invoice INV-330")),
    _t("invoice_sent", "Invoice sent", "General", U,
       "Hello {{1}}, invoice {{2}} for {{3}} has been sent to your email. Please make the payment by {{4}}.",
       ("Asha", "INV-330", "Rs 18,000", "20 October")),
    _t("order_cancelled", "Order cancelled", "General", U,
       "Hello {{1}}, your order {{2}} has been cancelled as you asked. If you paid already, the amount will come "
       "back to you within a few working days.", ("Asha", "ORD-1042")),
    _t("delivery_delay", "Delivery delayed", "General", U,
       "Hello {{1}}, your order {{2}} will reach you a little late, by about {{3}}. We are sorry for the delay.",
       ("Asha", "ORD-1042", "one day")),
    _t("complaint_received", "Complaint received", "General", U,
       "Hello {{1}}, we have received your complaint {{2}} and our team is looking into it. You will hear from us "
       "within {{3}}.", ("Asha", "CMP-118", "two working days")),
    _t("complaint_resolved", "Complaint resolved", "General", U,
       "Hello {{1}}, your complaint {{2}} has been resolved. Please tell us whether you are satisfied with the "
       "outcome.", ("Asha", "CMP-118"), buttons=(("qr", "Satisfied"), ("qr", "Not satisfied"))),
    _t("support_ticket_created", "Support ticket created", "General", U,
       "Hello {{1}}, we have opened ticket {{2}} for your request. Our support team will reply to you here as soon "
       "as possible.", ("Asha", "TKT-4410")),
    _t("birthday_greeting", "Birthday greeting", "General", M,
       "Hello {{1}}, wishing you a very happy birthday. Thank you for being a part of our journey.", ("Asha",),
       buttons=(STOP,)),
    _t("work_anniversary_customer", "Customer anniversary", "General", M,
       "Hello {{1}}, it has been {{2}} since you first worked with us. Thank you for your trust, and we look "
       "forward to many more years together.", ("Asha", "three years"), buttons=(STOP,)),
    _t("thank_you_purchase", "Thank you for your purchase", "General", M,
       "Hello {{1}}, thank you for choosing us for {{2}}. If you need any help with it, just reply to this "
       "message.", ("Asha", "your recent order")),
    _t("referral_invite", "Refer a friend", "General", M,
       "Hello {{1}}, if you know someone who needs {{2}}, send them our way. We will take good care of them, and "
       "thank you for spreading the word.", ("Asha", "our services"), buttons=(("qr", "I know someone"), STOP)),
    _t("subscription_renewal", "Subscription renewal", "General", U,
       "Hello {{1}}, your {{2}} subscription renews on {{3}}. You do not need to do anything, and you can change "
       "your plan from your account.", ("Asha", "monthly", "1 December")),
    _t("trial_ending", "Trial ending", "General", U,
       "Hello {{1}}, your free trial ends on {{2}}. Choose a plan before then to keep using everything without a "
       "break.", ("Asha", "25 October"), buttons=(("qr", "Show plans"), ("qr", "Talk to someone"))),
    _t("re_engagement", "We miss you", "General", M,
       "Hello {{1}}, it has been a while since we heard from you. If there is anything we can help with, we are "
       "only a message away.", ("Asha",), buttons=(("qr", "I need help"), STOP)),
    _t("survey_invite", "Short survey", "General", M,
       "Hello {{1}}, could you spare two minutes to answer a short survey about {{2}}? Your answers decide what we "
       "improve next.", ("Asha", "our service"), buttons=(("url", "Take the survey", "https://example.com/survey/{{1}}"), STOP)),
    _t("holiday_hours_notice", "Holiday hours", "General", U,
       "Hello {{1}}, our office will remain closed on {{2}} for the holiday. We will reply to your messages when "
       "we reopen.", ("Asha", "Diwali")),
    _t("account_update_notice", "Account update", "General", U,
       "Hello {{1}}, the details on your account were updated just now. If you did not ask for this change, please "
       "reply to this message straight away.", ("Asha",)),
    _t("appointment_rescheduled", "Appointment rescheduled", "General", U,
       "Hello {{1}}, your appointment has been moved to {{2}}. Please reply if this time does not suit you.",
       ("Asha", "Monday at 3 PM")),
    _t("document_request", "Document request", "General", U,
       "Hello {{1}}, please send us {{2}} so that we can complete your request. A clear photo or a scanned copy is "
       "fine.", ("Asha", "a copy of your address proof")),
    _t("welcome_back", "Welcome back", "General", U,
       "Hello {{1}}, welcome back. Tell us how we can help you today and we will get started right away.", ("Asha",)),
    # ── new kinds of template ────────────────────────────────────────────────
    _t("product_launch_picture", "Product launch with a picture", "General", M,
       "Hello {{1}}, meet {{2}}, our newest addition. Tap below to see all the details and the price.",
       ("Asha", "the compact work bench"), header_media="IMAGE", buttons=(("url", "See details", "https://example.com/new"), STOP)),
    _t("video_walkthrough", "Video walkthrough", "General", M,
       "Hello {{1}}, here is a short video walkthrough of {{2}}. Reply if you have any questions after watching it.",
       ("Asha", "our latest project"), header_media="VIDEO", buttons=(STOP,)),
    {"key": "festival_limited_offer", "title": "Limited-time festival offer", "industry": "General", "category": M, "language": "en_US",
     "needs_media": "image",
     "components": [{"type": "HEADER", "format": "IMAGE"},
                    _body("Hello {{1}}, our festival offer is on for a short time only. Use the code below at the time of "
                          "ordering to claim it.", "Asha"),
                    {"type": "LIMITED_TIME_OFFER", "limited_time_offer": {"text": "Festival offer", "has_expiration": True}},
                    {"type": "BUTTONS", "buttons": [{"type": "COPY_CODE", "text": "Copy offer code", "example": "FEST25"},
                                                    {"type": "URL", "text": "Order now", "url": "https://example.com/offer"}]}]},
    {"key": "carousel_new_arrivals", "title": "Carousel of new arrivals", "industry": "General", "category": M, "language": "en_US",
     "needs_media": "image",
     "components": [_body("Hello {{1}}, here are this week's new arrivals. Swipe through them and tap the one you like.", "Asha"),
                    {"type": "CAROUSEL", "cards": [
                        {"components": [{"type": "HEADER", "format": "IMAGE"},
                                        {"type": "BODY", "text": "Compact work bench in powder-coated steel"},
                                        {"type": "BUTTONS", "buttons": [{"type": "QUICK_REPLY", "text": "Tell me more"}]}]},
                        {"components": [{"type": "HEADER", "format": "IMAGE"},
                                        {"type": "BODY", "text": "Heavy duty storage rack with six shelves"},
                                        {"type": "BUTTONS", "buttons": [{"type": "QUICK_REPLY", "text": "Tell me more"}]}]}]}]},
    # ── authentication ───────────────────────────────────────────────────────
    {"key": "otp_one_tap", "title": "One-time password (one-tap autofill)", "industry": "Authentication", "category": A,
     "language": "en_US", "needs_app": True,
     "components": [{"type": "BODY", "add_security_recommendation": True}, {"type": "FOOTER", "code_expiration_minutes": 10},
                    {"type": "BUTTONS", "buttons": [{"type": "OTP", "otp_type": "ONE_TAP", "text": "Copy code", "autofill_text": "Autofill",
                                                     "package_name": "com.example.app", "signature_hash": "K8a/AX6ELnR"}]}]},
    {"key": "otp_zero_tap", "title": "One-time password (zero-tap)", "industry": "Authentication", "category": A,
     "language": "en_US", "needs_app": True,
     "components": [{"type": "BODY", "add_security_recommendation": True}, {"type": "FOOTER", "code_expiration_minutes": 10},
                    {"type": "BUTTONS", "buttons": [{"type": "OTP", "otp_type": "ZERO_TAP", "text": "Copy code", "autofill_text": "Autofill",
                                                     "package_name": "com.example.app", "signature_hash": "K8a/AX6ELnR",
                                                     "zero_tap_terms_accepted": True}]}]},
]

_CONSTRUCTION = {"quotation_sent", "quotation_followup", "site_visit_confirmation", "milestone_payment_reminder", "ra_bill_submitted",
                 "material_dispatch_update", "boq_ready"}
for _item in LIBRARY:
    _item.setdefault("industry", "Construction" if _item["key"] in _CONSTRUCTION else "Authentication" if _item["category"] == "AUTHENTICATION" else "General")
LIBRARY.extend(_MORE)
INDUSTRIES = sorted({i["industry"] for i in LIBRARY})
