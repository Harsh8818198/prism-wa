"""What each plan includes (spec §7.1) — as data, enforced in code.

The tiers mirror what the market sells (AiSensy's pricing page, read 27-Nov-2026): Free, Basic, Pro, Premium,
Unlimited and Enterprise, plus ADD-ONS bought on top of any plan — Chatbot flows, the AI agent and extra agent
seats. A workspace's plan is stored in `tenant_settings.settings.plan`; the licence server sets it from the
licence on every call (see its routes/whatsapp_platform.py), so the plan can never come from a client. An
unknown or missing plan is Basic, the lowest PAID tier, so a licence without a plan key never loses anything.

The stored value is the tier with its add-ons: "pro", or "pro+chatbot+ai_agent+seats3" (three extra agent
seats). `plan_of` returns that whole value; `base` takes the tier from it; `features_of` / `limits_of` read
both. A request that needs more than the plan gives is refused with `plan_limit` / `plan_feature` and says which
plan — or which add-on — has it.

Limits are numbers (distinct tags, custom attributes, saved segments, agents, messages a second); features
are named capabilities that either are or are not part of the plan. Prices are what the Compare plans screen
shows (`catalogue`): numbers to change here, in one place.
"""
from __future__ import annotations

from typing import Optional

ORDER = ("free", "basic", "pro", "premium", "unlimited", "enterprise")
DEFAULT_PLAN = "basic"
UNLIMITED = None

# Free (AiSensy's "Free Forever"): the API, templates, the live-chat inbox, the ads manager and contacts — no
# broadcasting. Templates and the inbox are not features here: every plan has them.
FREE_FEATURES = frozenset({"ads_manager", "integrations"})
BASIC_FEATURES = FREE_FEATURES | {"broadcast", "retargeting", "template_api", "multi_agent", "transfer", "monitor"}
PRO_FEATURES = BASIC_FEATURES | {"scheduler", "csv_scheduler", "click_tracking", "auto_retry", "smart_routing",
                                 "agent_rules", "access_control", "project_api", "budget_analytics",
                                 "carousel_click_tracking", "flow_delay", "flow_timeout", "commerce", "forms"}
PREMIUM_FEATURES = PRO_FEATURES | {"number_masking", "reports_download", "template_ttl", "webhooks",
                                   "duplicate_csv", "multi_ad_flow", "priority_support", "turbo_onboarding"}
UNLIMITED_FEATURES = PREMIUM_FEATURES | {"dedicated_manager", "white_label"}
# Enterprise is custom terms: everything, the add-ons included.
ENTERPRISE_FEATURES = UNLIMITED_FEATURES | {"sso", "audit_log", "custom_throughput", "sla_support", "flows",
                                            "ai_agent"}

LIMITS = {
    #              tags  attributes segments agents(incl. owner) msgs/sec  webhooks  flows  AI replies / month
    # flows and AI replies come with the add-ons (ADDONS below), not with a tier.
    "free":       {"tags": 10,  "attributes": 5,   "segments": 0,  "agents": 1,   "mps": 40,   "webhooks": 0,
                   "flows": 0, "ai_messages": 0, "storage_mb": 1024},
    "basic":      {"tags": 10,  "attributes": 5,   "segments": 2,  "agents": 6,   "mps": 40,   "webhooks": 0,
                   "flows": 0, "ai_messages": 0, "storage_mb": 1024},
    "pro":        {"tags": 100, "attributes": 20,  "segments": 10, "agents": 6,   "mps": 40,   "webhooks": 0,
                   "flows": 0, "ai_messages": 0, "storage_mb": 5120},
    "premium":    {"tags": 250, "attributes": 50,  "segments": 50, "agents": 10,  "mps": 250,  "webhooks": 1,
                   "flows": 0, "ai_messages": 0, "storage_mb": 5120},
    "unlimited":  {"tags": 500, "attributes": 100, "segments": 50, "agents": 201, "mps": 1000, "webhooks": 3,
                   "flows": 0, "ai_messages": 0, "storage_mb": 10240},
    "enterprise": {"tags": UNLIMITED, "attributes": UNLIMITED, "segments": UNLIMITED, "agents": UNLIMITED,
                   "mps": UNLIMITED, "webhooks": UNLIMITED, "flows": UNLIMITED, "ai_messages": UNLIMITED,
                   "storage_mb": UNLIMITED},
}
FEATURES = {"free": FREE_FEATURES, "basic": BASIC_FEATURES, "pro": PRO_FEATURES, "premium": PREMIUM_FEATURES,
            "unlimited": UNLIMITED_FEATURES, "enterprise": ENTERPRISE_FEATURES}
LABELS = {"free": "Free", "basic": "Basic", "pro": "Pro", "premium": "Premium", "unlimited": "Unlimited",
          "enterprise": "Enterprise"}

# Bought on top of any plan. Each adds features and raises limits (flows, AI replies a month).
ADDONS = {
    "chatbot": {"label": "Chatbot flows", "features": frozenset({"flows"}), "limits": {"flows": 5}},
    "ai_agent": {"label": "AI agent", "features": frozenset({"ai_agent"}), "limits": {"ai_messages": 1000}},
}
SEAT_PREFIX = "seats"                           # "seats3": three agent seats beyond the plan's own

# Rupees a month — the Compare plans screen. AiSensy's prices as the starting point (27-Nov-2026), the owner's
# to change. None = "talk to us". Quarterly and yearly take a discount, as AiSensy's do.
PRICES = {"free": 0, "basic": 1500, "pro": 3200, "premium": 9100, "unlimited": 45000, "enterprise": None}
ADDON_PRICES = {"chatbot": 2500, "ai_agent": 1350, "seat": 750}
BILLING_DISCOUNT = {"monthly": 0, "quarterly": 5, "yearly": 10}

TAGLINES = {"free": "WhatsApp API free for life", "basic": "Get started fast", "pro": "Growth marketer",
            "premium": "Expert marketer", "unlimited": "Everything, no limits", "enterprise": "Built for your scale"}
# The words the Compare plans screen shows for a feature, in the order it shows them.
FEATURE_LABELS = (
    ("broadcast", "Broadcasting & retargeting"), ("multi_agent", "Multi-agent live chat"),
    ("transfer", "Agent transfer & manager monitoring"), ("template_api", "Template message APIs"),
    ("ads_manager", "Click-to-WhatsApp ads manager"), ("integrations", "Shopify, WooCommerce & other integrations"),
    ("scheduler", "Campaign scheduler"), ("csv_scheduler", "CSV campaign scheduler"),
    ("click_tracking", "Campaign click tracking"), ("budget_analytics", "Campaign budget & analytics"),
    ("project_api", "Project APIs"), ("agent_rules", "Custom agent rules"), ("access_control", "User access control"),
    ("auto_retry", "Automatic failed-message retry"), ("flow_delay", "Time delay & timeout in chatbot flows"),
    ("commerce", "Shop, catalog & payments"), ("forms", "WhatsApp forms"),
    ("number_masking", "Number masking"), ("reports_download", "Download reports"), ("template_ttl", "Template TTL"),
    ("webhooks", "Webhooks"), ("duplicate_csv", "Duplicate contacts in a CSV broadcast"),
    ("multi_ad_flow", "Several ads into one chatbot flow"), ("priority_support", "Priority support"),
    ("turbo_onboarding", "Turbo onboarding"), ("dedicated_manager", "Dedicated account manager"),
    ("sso", "Company sign-in (SSO)"), ("audit_log", "Audit log"), ("sla_support", "SLA-backed support"),
)
LIMIT_LABELS = (("agents", "Agent seats (with the owner)"), ("mps", "Messages a second"), ("tags", "Tags"),
                ("attributes", "Custom attributes"), ("segments", "Audience segments"), ("webhooks", "Webhooks"),
                ("storage_mb", "Media storage (MB)"))


class PlanLimit(Exception):
    """The plan does not include this. `needs` is the lowest plan that does (None for a count limit
    when no plan has more), or "addon:<name>" when an add-on brings it."""

    def __init__(self, reason: str, message: str, needs: Optional[str] = None):
        super().__init__(message)
        self.reason = reason
        self.needs = needs


def _parts(plan: Optional[str]) -> tuple:
    """(tier, sorted add-ons, extra seats) from a stored or licence value. Unknown pieces are dropped."""
    bits = [b.strip().lower() for b in str(plan or "").split("+") if b.strip()]
    tier = bits[0] if bits and bits[0] in LIMITS else DEFAULT_PLAN
    addons, seats = set(), 0
    for b in bits[1:]:
        if b in ADDONS:
            addons.add(b)
        elif b.startswith(SEAT_PREFIX) and b[len(SEAT_PREFIX):].isdigit():
            seats += int(b[len(SEAT_PREFIX):])
    return tier, tuple(sorted(addons)), min(seats, 1000)


def normalise(plan: Optional[str]) -> str:
    """The canonical stored value: tier, then add-ons in a fixed order, then extra seats."""
    tier, addons, seats = _parts(plan)
    return "+".join([tier, *addons] + (["%s%d" % (SEAT_PREFIX, seats)] if seats else []))


def base(plan: Optional[str]) -> str:
    return _parts(plan)[0]


def addons_of(plan: Optional[str]) -> tuple:
    return _parts(plan)[1]


def seats_of(plan: Optional[str]) -> int:
    return _parts(plan)[2]


def features_of(plan: Optional[str]) -> frozenset:
    tier, addons, _ = _parts(plan)
    out = set(FEATURES[tier])
    for a in addons:
        out |= ADDONS[a]["features"]
    return frozenset(out)


def limits_of(plan: Optional[str]) -> dict:
    """The tier's ceilings, raised by its add-ons and extra seats. None stays unlimited."""
    tier, addons, seats = _parts(plan)
    out = dict(LIMITS[tier])
    for a in addons:
        for k, v in ADDONS[a]["limits"].items():
            if out.get(k) is not None:
                out[k] = (out[k] or 0) + v
    if seats and out.get("agents") is not None:
        out["agents"] += seats
    return out


def plan_of(cur, tenant_id) -> str:
    cur.execute("select settings->>'plan' from tenant_settings where tenant_id = %s", (tenant_id,))
    row = cur.fetchone()
    return normalise(row[0] if row else None)


def set_plan(cur, tenant_id, plan: str) -> str:
    """Record the workspace's plan (called with what the licence says). Returns the plan stored."""
    plan = normalise(plan)
    cur.execute("insert into tenant_settings (tenant_id, settings) values (%s, jsonb_build_object('plan', %s::text))"
                " on conflict (tenant_id) do update set settings = tenant_settings.settings"
                " || jsonb_build_object('plan', %s::text), updated_at = now()"
                " where tenant_settings.settings->>'plan' is distinct from %s", (tenant_id, plan, plan, plan))
    return plan


def lowest_with(feature: str) -> Optional[str]:
    return next((p for p in ORDER if feature in FEATURES[p]), None)


def addon_with(feature: str) -> Optional[str]:
    return next((a for a, spec in ADDONS.items() if feature in spec["features"]), None)


def has(plan: str, feature: str) -> bool:
    return feature in features_of(plan)


def allows(cur, tenant_id, feature: str) -> bool:
    return has(plan_of(cur, tenant_id), feature)


def require(cur, tenant_id, feature: str, what: str = "") -> None:
    plan = plan_of(cur, tenant_id)
    if has(plan, feature):
        return
    name = what or feature.replace("_", " ").capitalize()
    addon = addon_with(feature)
    if addon:
        raise PlanLimit("plan_feature", "%s comes with the %s add-on." % (name, ADDONS[addon]["label"]),
                        "addon:" + addon)
    needs = lowest_with(feature)
    raise PlanLimit("plan_feature", "%s is not part of the %s plan%s." % (
        name, LABELS[base(plan)], " (it starts on %s)" % LABELS[needs] if needs else ""), needs)


def limit(cur, tenant_id, kind: str) -> Optional[int]:
    return limits_of(plan_of(cur, tenant_id))[kind]


def check_count(cur, tenant_id, kind: str, current: int, adding: int = 1, what: str = "") -> None:
    """Refuse when `current + adding` would pass the plan's ceiling for `kind`."""
    plan = plan_of(cur, tenant_id)
    ceiling = limits_of(plan)[kind]
    if ceiling is not None and current + adding > ceiling:
        tier = base(plan)
        bigger = next((p for p in ORDER[ORDER.index(tier) + 1:] if LIMITS[p][kind] is None or LIMITS[p][kind] > ceiling),
                      None)
        addon = next((a for a, spec in ADDONS.items() if kind in spec["limits"] and a not in addons_of(plan)), None)
        more = ("; %s allows more" % LABELS[bigger]) if bigger else ""
        if addon and LIMITS[tier][kind] == 0:
            more, bigger = "; the %s add-on adds them" % ADDONS[addon]["label"], "addon:" + addon
        raise PlanLimit("plan_limit", "The %s plan allows %d %s%s." % (LABELS[tier], ceiling, what or kind, more), bigger)


def messages_per_second(cur, tenant_id) -> Optional[int]:
    return limit(cur, tenant_id, "mps")


def catalogue() -> dict:
    """Every tier and add-on as the Compare plans screen shows them: price, ceilings and features, in order."""
    tiers = []
    for p in ORDER:
        tiers.append({"plan": p, "label": LABELS[p], "tagline": TAGLINES[p], "price": PRICES[p],
                      "limits": {k: LIMITS[p][k] for k, _ in LIMIT_LABELS},
                      "features": [f for f, _ in FEATURE_LABELS if f in FEATURES[p]]})
    return {"tiers": tiers, "features": [{"key": k, "label": v} for k, v in FEATURE_LABELS],
            "limits": [{"key": k, "label": v} for k, v in LIMIT_LABELS],
            "addons": [{"addon": a, "label": spec["label"], "price": ADDON_PRICES[a],
                        "features": sorted(spec["features"]), "limits": spec["limits"]} for a, spec in ADDONS.items()]
            + [{"addon": "seat", "label": "Extra agent seat", "price": ADDON_PRICES["seat"], "features": [],
                "limits": {"agents": 1}}],
            "billing_discount": BILLING_DISCOUNT, "currency": "INR"}
