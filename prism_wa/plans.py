"""What each plan includes (spec §7.1) — as data, enforced in code.

The tiers mirror what the market sells: Basic, Pro, Premium, Unlimited (and Enterprise, which has
no ceilings). A workspace's plan is stored in `tenant_settings.settings.plan`; the licence server
sets it from the licence on every call (see its routes/whatsapp_platform.py), so the plan can
never come from a client. An unknown or missing plan is Basic, the lowest paid tier.

Limits are numbers (distinct tags, custom attributes, saved segments, agents, messages a second);
features are named capabilities that either are or are not part of the plan. A request that
needs more than the plan gives is refused with `plan_limit` and says which plan has it.

All figures are from the pricing page the owner pasted on 17-Oct-2026 (spec §7.1) and are
numbers to change in one place if that page changes.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

ORDER = ("basic", "pro", "premium", "unlimited", "enterprise")
DEFAULT_PLAN = "basic"
UNLIMITED = None

BASIC_FEATURES = frozenset({"broadcast", "retargeting", "template_api", "multi_agent", "transfer", "monitor",
                            "integrations", "ads_manager"})
PRO_FEATURES = BASIC_FEATURES | {"scheduler", "csv_scheduler", "click_tracking", "auto_retry", "smart_routing",
                                 "agent_rules", "access_control", "project_api", "budget_analytics", "flows",
                                 "commerce", "forms"}
PREMIUM_FEATURES = PRO_FEATURES | {"number_masking", "reports_download", "template_ttl", "webhooks",
                                   "duplicate_csv", "multi_ad_flow", "ai_agent"}

LIMITS = {
    #              tags  attributes segments agents(incl. owner) msgs/sec  webhooks  flows  AI replies / month
    # (flows and AI replies are this product's own allowances, not from the pricing page: change them here)
    "basic":      {"tags": 10,  "attributes": 5,   "segments": 2,  "agents": 6,   "mps": 40,   "webhooks": 0,
                   "flows": 0, "ai_messages": 0, "storage_mb": 1024},
    "pro":        {"tags": 100, "attributes": 20,  "segments": 10, "agents": 6,   "mps": 40,   "webhooks": 0,
                   "flows": 5, "ai_messages": 0, "storage_mb": 5120},
    "premium":    {"tags": 250, "attributes": 50,  "segments": 50, "agents": 10,  "mps": 250,  "webhooks": 1,
                   "flows": 20, "ai_messages": 1000, "storage_mb": 5120},
    "unlimited":  {"tags": 500, "attributes": 100, "segments": 50, "agents": 201, "mps": 1000, "webhooks": 3,
                   "flows": 50, "ai_messages": 5000, "storage_mb": 10240},
    "enterprise": {"tags": UNLIMITED, "attributes": UNLIMITED, "segments": UNLIMITED, "agents": UNLIMITED,
                   "mps": UNLIMITED, "webhooks": UNLIMITED, "flows": UNLIMITED, "ai_messages": UNLIMITED, "storage_mb": UNLIMITED},
}
FEATURES = {"basic": BASIC_FEATURES, "pro": PRO_FEATURES, "premium": PREMIUM_FEATURES,
            "unlimited": PREMIUM_FEATURES | {"dedicated_manager", "white_label"}, "enterprise": PREMIUM_FEATURES | {
                "dedicated_manager", "white_label", "sso", "audit_log", "custom_throughput", "sla_support"}}
LABELS = {"basic": "Basic", "pro": "Pro", "premium": "Premium", "unlimited": "Unlimited", "enterprise": "Enterprise"}


class PlanLimit(Exception):
    """The plan does not include this. `needs` is the lowest plan that does (None for a count limit
    when no plan has more)."""

    def __init__(self, reason: str, message: str, needs: Optional[str] = None):
        super().__init__(message)
        self.reason = reason
        self.needs = needs


def normalise(plan: Optional[str]) -> str:
    plan = (plan or "").strip().lower()
    return plan if plan in LIMITS else DEFAULT_PLAN


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


def has(plan: str, feature: str) -> bool:
    return feature in FEATURES[normalise(plan)]


def require(cur, tenant_id, feature: str, what: str = "") -> None:
    plan = plan_of(cur, tenant_id)
    if feature in FEATURES[plan]:
        return
    needs = lowest_with(feature)
    raise PlanLimit("plan_feature", "%s is not part of the %s plan%s." % (
        what or feature.replace("_", " ").capitalize(), LABELS[plan],
        " (it starts on %s)" % LABELS[needs] if needs else ""), needs)


def limit(cur, tenant_id, kind: str) -> Optional[int]:
    return LIMITS[plan_of(cur, tenant_id)][kind]


def check_count(cur, tenant_id, kind: str, current: int, adding: int = 1, what: str = "") -> None:
    """Refuse when `current + adding` would pass the plan's ceiling for `kind`."""
    plan = plan_of(cur, tenant_id)
    ceiling = LIMITS[plan][kind]
    if ceiling is not None and current + adding > ceiling:
        bigger = next((p for p in ORDER[ORDER.index(plan) + 1:] if LIMITS[p][kind] is None or LIMITS[p][kind] > ceiling), None)
        raise PlanLimit("plan_limit", "The %s plan allows %d %s%s." % (
            LABELS[plan], ceiling, what or kind, "; %s allows more" % LABELS[bigger] if bigger else ""), bigger)


def messages_per_second(cur, tenant_id) -> Optional[int]:
    return limit(cur, tenant_id, "mps")
