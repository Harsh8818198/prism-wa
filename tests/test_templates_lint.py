"""Template lint and the ready-made library. No database."""
import pytest

from prism_wa import template_library, templates
from prism_wa.templates import lint


def body(text, *samples):
    return {"type": "BODY", "text": text, "example": {"body_text": [list(samples)]}}


def run(components, name="order_update", language="en_US", category="UTILITY"):
    return lint(name=name, language=language, category=category, components=components)


def codes(issues, severity=None):
    return {i.code for i in issues if severity in (None, i.severity)}


GOOD = body("Hello there, your order has been packed and will leave our warehouse tomorrow morning.")


def with_samples(components):
    """What the editor adds when the person chooses a sample file: a handle for each media header, in a card too."""
    out = []
    for c in components:
        c = dict(c)
        if c.get("type") == "HEADER" and c.get("format") in ("IMAGE", "VIDEO", "DOCUMENT"):
            c["example"] = {"header_handle": ["4::sample"]}
        if c.get("type") == "CAROUSEL":
            c["cards"] = [{"components": with_samples(card["components"])} for card in c["cards"]]
        out.append(c)
    return out


def test_the_whole_library_is_clean_and_every_key_is_unique():
    keys = [t["key"] for t in template_library.LIBRARY]
    assert len(keys) == len(set(keys)) >= 100
    for t in template_library.LIBRARY:
        issues = lint(name=t["key"], language=t["language"], category=t["category"], components=with_samples(t["components"]))
        assert issues == [], (t["key"], [i.message for i in issues])
    assert template_library.get("boq_ready")["category"] == "UTILITY"
    with pytest.raises(KeyError):
        template_library.get("nope")


def test_every_library_entry_has_an_industry_and_there_are_a_dozen_of_them():
    assert all(t["industry"] for t in template_library.LIBRARY)
    assert len(template_library.INDUSTRIES) >= 12 and "Construction" in template_library.INDUSTRIES
    by = {i: [t for t in template_library.LIBRARY if t["industry"] == i] for i in template_library.INDUSTRIES}
    assert all(len(v) >= 2 for v in by.values()) and len(by["Construction"]) >= 15
    assert {t["category"] for t in template_library.LIBRARY} == {"MARKETING", "UTILITY", "AUTHENTICATION"}


def test_library_entries_that_need_a_file_or_an_app_say_so():
    need = {t["key"]: t for t in template_library.LIBRARY if t.get("needs_media")}
    assert {"brochure_share", "product_launch_picture", "video_walkthrough", "festival_limited_offer", "carousel_new_arrivals"} <= set(need)
    assert need["video_walkthrough"]["needs_media"] == "video" and template_library.get("otp_one_tap")["needs_app"]


def test_a_plain_body_is_fine():
    assert run([GOOD]) == []


@pytest.mark.parametrize("name", ["Order Update", "order-update", "", "x" * 513, "ऑर्डर"])
def test_names_must_be_lowercase_alphanumeric_and_underscore(name):
    assert "bad_name" in codes(run([GOOD], name=name))


@pytest.mark.parametrize("language", ["", "english", "EN_us", "en-US"])
def test_languages_must_look_like_meta_codes(language):
    assert "bad_language" in codes(run([GOOD], language=language))


def test_an_unknown_category_is_refused():
    assert "bad_category" in codes(run([GOOD], category="PROMO"))


def test_no_components_or_no_body():
    assert "no_components" in codes(run([]))
    assert "no_body" in codes(run([{"type": "FOOTER", "text": "x"}]))
    assert "empty_body" in codes(run([{"type": "BODY", "text": "   "}]))
    assert "duplicate_component" in codes(run([GOOD, GOOD]))
    assert "bad_component" in codes(run([GOOD, {"type": "SIDEBAR"}]))


def test_body_length_limit():
    assert "body_too_long" in codes(run([body("word " * 300)]))
    assert "body_too_long" not in codes(run([body("a" * 1024)]))


def test_variables_must_run_one_two_three_with_a_sample_each():
    ok = body("Hi there friend, your order {{1}} is due on {{2}} so please keep your phone handy.", "A1", "Monday")
    assert run([ok]) == []
    assert "variables_not_sequential" in codes(run([body("Hi there friend, order {{2}} is due on {{1}} so keep your phone handy.", "a", "b")]))
    assert "variables_not_sequential" in codes(run([body("Hi there friend, order {{1}} and {{1}} are due so keep your phone handy.", "a", "a")]))
    assert "missing_example" in codes(run([body("Hi there friend, your order {{1}} is due on {{2}} so please keep your phone handy.", "only one")]))
    assert "missing_example" in codes(run([body("Hi there friend, your order {{1}} is due on {{2}} so please keep your phone handy.", "A1", "  ")]))
    assert "missing_example" in codes(run([{"type": "BODY", "text": "Hi there friend, your order {{1}} is due on Monday so keep your phone handy."}]))


def test_a_variable_at_the_start_or_end_is_error_2388299():
    start = run([body("{{1}} your order is ready and waiting for you at the front desk today", "Asha")])
    end = run([body("Your order is ready and waiting for you at the front desk today, {{1}}", "Asha")])
    assert "variable_at_start" in codes(start, "error") and "variable_at_end" in codes(end, "error")
    assert {i.meta_code for i in start + end if i.severity == "error"} == {templates.ERR_VARIABLE_AT_EDGE}
    trailing = run([body("Your order is ready and waiting for you at the front desk today, {{1}}.", "Asha")])
    assert "variable_at_end" not in codes(trailing)


def test_too_many_variables_for_the_length_is_only_a_warning():
    issues = run([body("Hi {{1}} order {{2}} total {{3}} due {{4}} thanks", "a", "b", "c", "d")])
    assert "variable_ratio" in codes(issues, "warning") and not templates.errors(issues)
    assert [i.meta_code for i in issues if i.code == "variable_ratio"] == [templates.ERR_VARIABLE_RATIO]


def test_header_rules():
    assert run([{"type": "HEADER", "format": "TEXT", "text": "Order ready"}, GOOD]) == []
    assert "header_too_long" in codes(run([{"type": "HEADER", "format": "TEXT", "text": "x" * 61}, GOOD]))
    assert "header_variables" in codes(run([{"type": "HEADER", "format": "TEXT", "text": "{{1}} and {{2}}"}, GOOD]))
    assert "missing_example" in codes(run([{"type": "HEADER", "format": "TEXT", "text": "Order {{1}}"}, GOOD]))
    assert run([{"type": "HEADER", "format": "TEXT", "text": "Order {{1}}", "example": {"header_text": ["A1"]}}, GOOD]) == []
    assert "missing_example" in codes(run([{"type": "HEADER", "format": "IMAGE"}, GOOD]))
    assert run([{"type": "HEADER", "format": "IMAGE", "example": {"header_handle": ["4::aW1hZ2U"]}}, GOOD]) == []
    assert run([{"type": "HEADER", "format": "LOCATION"}, GOOD]) == []
    assert "bad_header_format" in codes(run([{"type": "HEADER", "format": "GIF"}, GOOD]))
    assert "empty_header" in codes(run([{"type": "HEADER", "format": "TEXT", "text": " "}, GOOD]))


def test_footer_rules():
    assert "footer_too_long" in codes(run([GOOD, {"type": "FOOTER", "text": "x" * 61}]))
    assert "footer_variables" in codes(run([GOOD, {"type": "FOOTER", "text": "Thanks {{1}}"}]))


def buttons(*bs):
    return {"type": "BUTTONS", "buttons": list(bs)}


QR = lambda t="OK": {"type": "QUICK_REPLY", "text": t}                  # noqa: E731
URL = lambda u="https://example.com/track": {"type": "URL", "text": "Track", "url": u}   # noqa: E731


def test_button_rules():
    assert run([GOOD, buttons(QR("Yes"), QR("No"), URL(), {"type": "PHONE_NUMBER", "text": "Call", "phone_number": "+919876543210"})]) == []
    assert "button_text_too_long" in codes(run([GOOD, buttons(QR("x" * 26))]))
    assert "button_text_empty" in codes(run([GOOD, buttons(QR(" "))]))
    assert "no_buttons" in codes(run([GOOD, {"type": "BUTTONS", "buttons": []}]))
    assert "too_many_buttons" in codes(run([GOOD, buttons(*[QR("b%d" % i) for i in range(11)])]))
    assert "too_many_urls" in codes(run([GOOD, buttons(URL(), URL("https://example.com/b"), URL("https://example.com/c"))]))
    assert "too_many_phones" in codes(run([GOOD, buttons(*[{"type": "PHONE_NUMBER", "text": "Call", "phone_number": "+919876543210"}] * 2)]))
    assert "bad_phone" in codes(run([GOOD, buttons({"type": "PHONE_NUMBER", "text": "Call", "phone_number": "98765"})]))
    assert "bad_url" in codes(run([GOOD, buttons(URL("example.com"))]))
    assert "bad_button" in codes(run([GOOD, buttons({"type": "LASER", "text": "x"})]))
    assert "bad_url_variable" in codes(run([GOOD, buttons(URL("https://example.com/{{1}}/more"))]))
    assert "missing_example" in codes(run([GOOD, buttons(URL("https://example.com/t/{{1}}"))]))
    assert run([GOOD, buttons({"type": "URL", "text": "Track", "url": "https://example.com/t/{{1}}", "example": ["abc"]})]) == []
    assert "missing_example" in codes(run([GOOD, buttons({"type": "COPY_CODE", "text": "Copy"})]))
    assert "quick_replies_not_together" in codes(run([GOOD, buttons(QR("a"), URL(), QR("b"))]), "warning")


def test_authentication_templates_have_a_fixed_body_and_an_otp_button():
    ok = [{"type": "BODY", "add_security_recommendation": True},
          buttons({"type": "OTP", "otp_type": "COPY_CODE", "text": "Copy code"})]
    assert run(ok, category="AUTHENTICATION") == []
    assert "auth_needs_otp_button" in codes(run([{"type": "BODY", "add_security_recommendation": True}], category="AUTHENTICATION"))
    assert "auth_body_fixed" in codes(run([{"type": "BODY", "text": "Your code is {{1}}"}, buttons({"type": "OTP", "text": "x"})],
                                          category="AUTHENTICATION"))


def card(buttons_=("QUICK_REPLY",), fmt="IMAGE"):
    return {"components": [{"type": "HEADER", "format": fmt, "example": {"header_handle": ["h"]}},
                           {"type": "BODY", "text": "A product card with enough words in it"},
                           {"type": "BUTTONS", "buttons": [{"type": b, "text": "Go"} for b in buttons_]}]}


def test_carousel_rules():
    intro = body("Here are a few of our favourite products picked just for you this week.")
    ok = [intro, {"type": "CAROUSEL", "cards": [card(), card()]}]
    assert run(ok, category="MARKETING") == []
    assert "bad_card_count" in codes(run([intro, {"type": "CAROUSEL", "cards": [card()]}], category="MARKETING"))
    assert "bad_card_count" in codes(run([intro, {"type": "CAROUSEL", "cards": [card()] * 11}], category="MARKETING"))
    assert "card_needs_media" in codes(run([intro, {"type": "CAROUSEL", "cards": [card(fmt="TEXT"), card()]}], category="MARKETING"))
    assert "card_buttons_differ" in codes(run([intro, {"type": "CAROUSEL", "cards": [card(), card(("URL",))]}], category="MARKETING"))
    assert "no_body" in codes(run([{"type": "CAROUSEL", "cards": [card(), card()]}], category="MARKETING"))


def test_malformed_input_never_raises():
    for components in (None, "x", [None], [1, 2], [{"type": "BUTTONS"}], [{"type": "BODY", "text": None}],
                       [GOOD, {"type": "BUTTONS", "buttons": [None, 3]}]):
        lint(name="ok_name", language="en_US", category="UTILITY", components=components)


# ── authentication ───────────────────────────────────────────────────────────

def auth(button, **footer):
    return [{"type": "BODY", "add_security_recommendation": True}, {"type": "FOOTER", **({"code_expiration_minutes": 10} | footer)},
            {"type": "BUTTONS", "buttons": [button]}]


COPY = {"type": "OTP", "otp_type": "COPY_CODE", "text": "Copy code"}
ONE = {"type": "OTP", "otp_type": "ONE_TAP", "text": "Copy code", "autofill_text": "Autofill", "package_name": "com.example.app", "signature_hash": "K8a/AX6ELnR"}
ZERO = {**ONE, "otp_type": "ZERO_TAP", "zero_tap_terms_accepted": True}


def test_each_kind_of_code_button_is_accepted():
    for button in (COPY, ONE, ZERO):
        assert run(auth(button), category="AUTHENTICATION") == [], button["otp_type"]


@pytest.mark.parametrize("change,code", [
    ({"otp_type": "SMS"}, "bad_otp_type"), ({"package_name": "notapackage"}, "bad_package"), ({"package_name": ""}, "bad_package"),
    ({"signature_hash": "short"}, "bad_signature_hash"), ({"autofill_text": ""}, "autofill_text_empty"),
    ({"autofill_text": "x" * 26}, "button_text_too_long"), ({"text": "x" * 26}, "button_text_too_long")])
def test_a_one_tap_button_must_be_complete(change, code):
    assert code in codes(run(auth({**ONE, **change}), category="AUTHENTICATION"), "error")


def test_zero_tap_needs_the_terms_accepted():
    assert "zero_tap_terms" in codes(run(auth({**ZERO, "zero_tap_terms_accepted": False}), category="AUTHENTICATION"), "error")
    assert "zero_tap_terms" in codes(run(auth({k: v for k, v in ZERO.items() if k != "zero_tap_terms_accepted"}), category="AUTHENTICATION"), "error")


def test_an_authentication_template_is_the_one_button_with_no_words_of_its_own():
    two = [{"type": "BODY", "add_security_recommendation": True}, {"type": "BUTTONS", "buttons": [COPY, COPY]}]
    assert "auth_one_button" in codes(run(two, category="AUTHENTICATION"), "error")
    assert "auth_no_header" in codes(run([{"type": "HEADER", "format": "TEXT", "text": "Code"}] + auth(COPY), category="AUTHENTICATION"), "error")
    assert "auth_footer_fixed" in codes(run([{"type": "BODY", "add_security_recommendation": True}, {"type": "FOOTER", "text": "hi"},
                                             {"type": "BUTTONS", "buttons": [COPY]}], category="AUTHENTICATION"), "error")
    assert "no_body" in codes(run([{"type": "BUTTONS", "buttons": [COPY]}], category="AUTHENTICATION"), "error")


@pytest.mark.parametrize("minutes,ok", [(1, True), (10, True), (90, True), (0, False), (91, False), (-5, False), ("10", False), (True, False)])
def test_the_code_expiry_is_one_to_ninety_minutes(minutes, ok):
    got = codes(run(auth(COPY, code_expiration_minutes=minutes), category="AUTHENTICATION"), "error")
    assert ("bad_code_expiry" not in got) is ok


# ── limited-time offer ───────────────────────────────────────────────────────

def offer(text="Festival offer", header="IMAGE", buttons=None, **extra):
    comps = [{"type": "HEADER", "format": header, "example": {"header_handle": ["4::s"]}},
             body("Hello {{1}}, our festival offer is on for a short time only, so use the code below to claim it.", "Asha"),
             {"type": "LIMITED_TIME_OFFER", "limited_time_offer": {"text": text, "has_expiration": True, **extra}}]
    if buttons is not False:
        comps.append({"type": "BUTTONS", "buttons": buttons or [{"type": "COPY_CODE", "text": "Copy code", "example": "FEST25"}]})
    return comps


def test_a_limited_time_offer_is_a_marketing_template_with_a_picture_or_video():
    assert run(offer(), category="MARKETING") == []
    assert run(offer(header="VIDEO"), category="MARKETING") == []
    assert "offer_is_marketing" in codes(run(offer(), category="UTILITY"), "error")
    assert "offer_needs_media" in codes(run(offer(header="DOCUMENT"), category="MARKETING"), "error")


def test_the_offer_text_is_short_and_required():
    assert "offer_text_too_long" in codes(run(offer(text="x" * 17), category="MARKETING"), "error")
    assert "offer_text_empty" in codes(run(offer(text="  "), category="MARKETING"), "error")
    assert "offer_expiry_flag" in codes(run(offer(has_expiration="yes"), category="MARKETING"), "error")


def test_an_offer_without_a_coupon_or_link_is_advised_against():
    assert "offer_needs_action" in codes(run(offer(buttons=False), category="MARKETING"), "warning")
    assert "offer_needs_action" not in codes(run(offer(buttons=[{"type": "URL", "text": "Shop", "url": "https://x.in/a"}]), category="MARKETING"))


# ── carousel ─────────────────────────────────────────────────────────────────

def pcard(text="Compact work bench in powder-coated steel", fmt="IMAGE", handle=True, buttons=None):
    head = {"type": "HEADER", "format": fmt}
    if handle:
        head["example"] = {"header_handle": ["4::s"]}
    return {"components": [head, {"type": "BODY", "text": text}, {"type": "BUTTONS", "buttons": buttons or [{"type": "QUICK_REPLY", "text": "Tell me more"}]}]}


def carousel(*cards):
    return [body("Hello {{1}}, here are this week's new arrivals. Swipe through them and tap the one you like.", "Asha"),
            {"type": "CAROUSEL", "cards": list(cards)}]


def test_a_carousel_of_two_to_ten_matching_cards_is_fine():
    assert run(carousel(pcard(), pcard()), category="MARKETING") == []
    assert "bad_card_count" in codes(run(carousel(pcard()), category="MARKETING"), "error")
    assert "bad_card_count" in codes(run(carousel(*[pcard()] * 11), category="MARKETING"), "error")
    assert run(carousel(*[pcard()] * 10), category="MARKETING") == []


def test_each_card_needs_its_sample_a_short_text_and_at_most_two_buttons():
    assert "missing_example" in codes(run(carousel(pcard(handle=False), pcard()), category="MARKETING"), "error")
    assert "card_body_too_long" in codes(run(carousel(pcard(text="x" * 161), pcard()), category="MARKETING"), "error")
    assert "card_body_empty" in codes(run(carousel(pcard(text="  "), pcard()), category="MARKETING"), "error")
    two = [{"type": "QUICK_REPLY", "text": "A"}, {"type": "QUICK_REPLY", "text": "B"}, {"type": "QUICK_REPLY", "text": "C"}]
    assert "card_too_many_buttons" in codes(run(carousel(pcard(buttons=two), pcard(buttons=two)), category="MARKETING"), "error")
    copy = [{"type": "COPY_CODE", "text": "x"}]
    assert "bad_button" in codes(run(carousel(pcard(buttons=copy), pcard(buttons=copy)), category="MARKETING"), "error")


def test_cards_must_match_each_other_in_buttons_and_media():
    other = [{"type": "URL", "text": "Open", "url": "https://x.in/a"}]
    assert "card_buttons_differ" in codes(run(carousel(pcard(), pcard(buttons=other)), category="MARKETING"), "error")
    assert "card_media_differs" in codes(run(carousel(pcard(), pcard(fmt="VIDEO")), category="MARKETING"), "error")


# ── message validity (TTL) ───────────────────────────────────────────────────

@pytest.mark.parametrize("category,ttl,ok", [
    ("MARKETING", 12 * 3600, True), ("MARKETING", 30 * 86400, True), ("MARKETING", 3600, False), ("MARKETING", 31 * 86400, False),
    ("UTILITY", 30, True), ("UTILITY", 12 * 3600, True), ("UTILITY", 29, False), ("UTILITY", 13 * 3600, False),
    ("AUTHENTICATION", 30, True), ("AUTHENTICATION", 900, True), ("AUTHENTICATION", 901, False),
    ("UTILITY", None, True), ("UTILITY", -1, True), ("UTILITY", "600", False), ("UTILITY", True, False)])
def test_message_validity_has_a_range_per_category(category, ttl, ok):
    comps = auth(COPY) if category == "AUTHENTICATION" else [GOOD]
    got = codes(lint(name="order_update", language="en_US", category=category, components=comps, ttl_seconds=ttl), "error")
    assert ("bad_ttl" not in got) is ok


def test_a_validity_error_says_the_range_in_words():
    msg = next(i.message for i in lint(name="a", language="en_US", category="MARKETING", components=[GOOD], ttl_seconds=60) if i.code == "bad_ttl")
    assert "12 hours" in msg and "30 days" in msg


# ── a utility message that reads like a promotion ────────────────────────────

@pytest.mark.parametrize("text,flagged", [
    ("Hello {{1}}, our diwali offer on steel gates ends on Friday so please reply soon.", True),
    ("Hello there, enjoy 20% off on every order placed before the end of this week.", True),
    ("Hello there, your order has been packed and will leave our warehouse tomorrow morning.", False),
    ("Hello there, there is a discounted rate for you on the next project we do.", False)])
def test_a_utility_message_that_sounds_promotional_is_warned_about(text, flagged):
    samples = ("Asha",) if "{{1}}" in text else ()
    got = codes(run([body(text, *samples)], category="UTILITY"), "warning")
    assert ("looks_promotional" in got) is flagged
    assert "looks_promotional" not in codes(run([body(text, *samples)], category="MARKETING"))


def test_a_location_header_needs_no_sample():
    assert run([{"type": "HEADER", "format": "LOCATION"}, GOOD]) == []
