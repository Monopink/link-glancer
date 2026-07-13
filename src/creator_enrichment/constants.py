from __future__ import annotations

from link_glancer.tasks.export_fields import KNOWN_CONTACT_EXPORT_FIELDS

PROFILE_API_PATH = "/api/v1/oec/affiliate/creator/marketplace/profile"
CONTACT_API_PATH = "/api_sens/v1/affiliate/cmp/contact"
DETAIL_URL_TEMPLATE = (
    "https://affiliate.tiktokshopglobalselling.com/connection/creator/detail?cid={creator_oecuid}"
)

PROFILE_WAIT_SECONDS = 8
CONTACT_BADGE_WAIT_SECONDS = 5
CONTACT_WAIT_SECONDS = 6
CONTACT_BADGE_SCROLL_TIMEOUT_MS = 150
CONTACT_BADGE_CLICK_TIMEOUT_MS = 250
CONTACT_CLICK_RETRY_SECONDS = 1
FAILURE_RETRY_LIMIT = 3
STATUS_PUSH_INTERVAL_SECONDS = 0.5
POLL_INTERVAL_SECONDS = 0.2
PLAYWRIGHT_ALLOWED_DEFAULT_ARGS = [
    "--no-sandbox",
    "--disable-extensions",
    "--disable-component-extensions-with-background-pages",
]

PAUSE_REASON_CAPTCHA = "captcha"
PAUSE_REASON_REGION_MISMATCH = "region_mismatch"
PAUSE_REASON_MANUAL_ACTION = "manual_action"

STATE_STATUS_PENDING = "pending"
STATE_STATUS_SUCCESS = "success"
STATE_STATUS_NO_CONTACT = "no_contact"
STATE_STATUS_PAUSED_CAPTCHA = "paused_captcha"
STATE_STATUS_PAUSED_REGION_MISMATCH = "paused_region_mismatch"
STATE_STATUS_PAUSED_MANUAL_ACTION = "paused_manual_action"
STATE_STATUS_SKIPPED = "skipped"

KNOWN_CONTACT_FIELD_MAP = {
    1: "whatsapp",
    2: "email",
    31: "line",
    32: "zalo",
    33: "viber",
    34: "facebook",
}
KNOWN_CONTACT_FIELDS_IN_ORDER = list(KNOWN_CONTACT_EXPORT_FIELDS)
TERMINAL_ENRICHMENT_STATUSES = {
    STATE_STATUS_SUCCESS,
    STATE_STATUS_NO_CONTACT,
    STATE_STATUS_SKIPPED,
}
