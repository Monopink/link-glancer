from __future__ import annotations

from datetime import UTC, datetime
from time import monotonic, sleep

from playwright.sync_api import Error as PlaywrightError

from creator_enrichment.parsers import normalized_creator_id

CONTACT_ICON_ROOT_SELECTOR = "#creator-detail-profile-container [data-e2e='73fc4445-a755-a7ab']"
CONTACT_ICON_BUTTON_SELECTOR = "div.inline-block > [data-e2e='0af7a642-88d7-376d']"
CONTACT_ICON_BUTTON_ITEM_SELECTOR = ":scope > [data-e2e='f9158724-e9b3-bca4'].cursor-pointer"
ALLOWED_CONTACT_ICON_CLASSES = (
    "alliance-icon-email",
    "alliance-icon-facebook_circle",
    "alliance-icon-line_circle",
    "alliance-icon-phone",
    "alliance-icon-viber_circle",
    "alliance-icon-zalo_circle",
)
UNEXPECTED_TAB_BREACH_REASONS = frozenset({"context_page_opened", "popup_opened"})
POST_CLICK_SETTLE_SECONDS = 0.75


class ContactBadgeMixin:
    def _click_contact_badge(self) -> bool:
        if self._page is None:
            return False
        self._contact_badge_click_failure_reason = ""
        if self._current_step != "waiting_contact_badge":
            return self._skip_contact_badge_click(reason="step_mismatch")
        if self._captured_contact is not None:
            return self._skip_contact_badge_click(reason="contact_already_captured")
        if getattr(self, "_contact_badge_click_inflight", False):
            return self._skip_contact_badge_click(reason="click_inflight")
        if not self._current_page_matches_current_item():
            self._last_contact_badge_strategy = "dom:page_mismatch"
            return False
        if self._last_contact_badge_clicked:
            return self._skip_contact_badge_click(reason="already_clicked")
        self._contact_badge_click_inflight = True
        try:
            result = self._click_contact_badge_via_dom()
            if result["clicked"]:
                self._last_contact_badge_detected = bool(result["detected"])
                self._last_contact_badge_clicked = True
                self._last_contact_badge_strategy = str(result["strategy"] or "")
                self._last_contact_badge_clicked_at = datetime.now(UTC)
                self._contact_positive_signal = True
                self._stabilize_page_after_contact_click(reason="dom_click")
                self._log_event(
                    "badge_click_success",
                    task_index=self._current_task_index,
                    strategy=self._last_contact_badge_strategy,
                    click_mode=str(result.get("click_mode") or ""),
                    candidate_index=int(result.get("candidate_index") or 0),
                    matched_icon_class=str(result.get("matched_icon_class") or ""),
                    candidate_count=int(result.get("candidate_count") or 0),
                    candidate_summary=str(result.get("candidate_summary") or ""),
                    page_count_before=int(result.get("page_count_before") or 0),
                    page_count_after=int(result.get("page_count_after") or 0),
                    unexpected_new_page=bool(result.get("unexpected_new_page")),
                    page_guard_breach_reason=str(result.get("page_guard_breach_reason") or ""),
                    page_url=self._safe_page_url(),
                )
                return True
            self._last_contact_badge_detected = bool(result["detected"])
            if result["strategy"]:
                self._last_contact_badge_strategy = str(result["strategy"])
            self._log_event(
                "badge_click_attempt",
                task_index=self._current_task_index,
                detected=self._last_contact_badge_detected,
                clicked=False,
                strategy=self._last_contact_badge_strategy,
                click_mode=str(result.get("click_mode") or ""),
                candidate_index=int(result.get("candidate_index") or 0),
                matched_icon_class=str(result.get("matched_icon_class") or ""),
                candidate_count=int(result.get("candidate_count") or 0),
                candidate_summary=str(result.get("candidate_summary") or ""),
                page_count_before=int(result.get("page_count_before") or 0),
                page_count_after=int(result.get("page_count_after") or 0),
                unexpected_new_page=bool(result.get("unexpected_new_page")),
                page_guard_breach_reason=str(result.get("page_guard_breach_reason") or ""),
                click_error=str(result.get("click_error") or ""),
            )
            return False
        finally:
            self._contact_badge_click_inflight = False

    def _skip_contact_badge_click(self, *, reason: str) -> bool:
        self._last_contact_badge_strategy = f"dom:{reason}"
        self._log_event(
            "badge_click_skipped",
            task_index=self._current_task_index,
            reason=reason,
            step=self._current_step,
            page_url=self._safe_page_url(),
        )
        return False

    def _stabilize_page_after_contact_click(self, *, reason: str) -> None:
        if self._current_task_index is None:
            return
        if not self._ensure_single_work_page(reason=f"{reason}:page_guard"):
            return
        target_url = self._detail_url_for_task(self._current_task_index)
        if not target_url or self._page is None:
            return
        if self._current_page_matches_current_item():
            return
        self._log_event(
            "page_drift_after_click",
            reason=reason,
            task_index=self._current_task_index,
            page_url=self._safe_page_url(),
            target_url=target_url,
        )
        self._repair_work_page_if_needed(
            reason=f"{reason}:repair",
            task_index=self._current_task_index,
            target_url=target_url,
        )

    def _click_contact_badge_via_dom(self) -> dict[str, object]:
        if self._page is None:
            return {"detected": False, "clicked": False, "strategy": "dom:no_page"}
        try:
            result = self._page.evaluate(
                """
                ({ rootSelector, buttonSelector, buttonItemSelector, allowedIconClasses }) => {
                    const root = document.querySelector(rootSelector);
                    if (!(root instanceof HTMLElement)) {
                        return {
                            detected: false,
                            clicked: false,
                            strategy: "dom:root_missing",
                            clickMode: "dom_scan",
                            candidateIndex: -1,
                            matchedIconClass: "",
                            candidate_count: 0,
                            candidate_summary: "",
                        };
                    }
                    const isVisible = (element) => {
                        if (!(element instanceof HTMLElement)) {
                            return false;
                        }
                        const style = window.getComputedStyle(element);
                        if (style.display === "none" || style.visibility === "hidden") {
                            return false;
                        }
                        const rect = element.getBoundingClientRect();
                        return rect.width > 0 && rect.height > 0;
                    };
                    const classTokens = (element) => {
                        if (!(element instanceof Element)) {
                            return [];
                        }
                        const rawClassName =
                            typeof element.className === "string"
                                ? element.className
                                : element.className?.baseVal || "";
                        return rawClassName
                            .split(/\\s+/)
                            .map((token) => token.trim().toLowerCase())
                            .filter(Boolean);
                    };
                    const iconKind = (tokens) => {
                        for (const token of tokens) {
                            if (allowedIconClasses.includes(token)) {
                                return token;
                            }
                        }
                        return "";
                    };
                    const describeTarget = (target, iconTokens) => {
                        if (!(target instanceof HTMLElement)) {
                            return "";
                        }
                        const className =
                            typeof target.className === "string" ? target.className.trim() : "";
                        return [
                            target.tagName.toLowerCase() || "",
                            target.getAttribute("data-e2e") || "",
                            target.getAttribute("data-tid") || "",
                            target.getAttribute("aria-label") || "",
                            target.getAttribute("title") || "",
                            className,
                            iconTokens.join(","),
                        ]
                            .map((part) =>
                                typeof part === "string" ? part.trim().toLowerCase() : "",
                            )
                            .filter(Boolean)
                            .join("|");
                    };
                    const group = root.querySelector(buttonSelector);
                    if (!(group instanceof HTMLElement)) {
                        return {
                            detected: false,
                            clicked: false,
                            strategy: "dom:contact_icon:group_missing",
                            clickMode: "dom_scan",
                            candidateIndex: -1,
                            matchedIconClass: "",
                            candidate_count: 0,
                            candidate_summary: "",
                        };
                    }
                    const candidates = Array.from(
                        group.querySelectorAll(buttonItemSelector),
                    ).filter((node) => node instanceof HTMLElement);
                    const candidateSummary = candidates
                        .slice(0, 6)
                        .map(
                            (candidate) => {
                                const svg = candidate.querySelector("svg");
                                const iconTokens = classTokens(svg);
                                return describeTarget(candidate, iconTokens);
                            },
                        )
                        .join(" || ");
                    let hiddenAllowedCount = 0;
                    let allowedCandidateCount = 0;
                    let allowedCandidateIndex = -1;
                    for (const [index, candidate] of candidates.entries()) {
                        if (!(candidate instanceof HTMLElement)) {
                            continue;
                        }
                        const svg = candidate.querySelector("svg");
                        const iconTokens = classTokens(svg);
                        const matchedIconKind = iconKind(iconTokens);
                        if (!matchedIconKind) {
                            continue;
                        }
                        allowedCandidateCount += 1;
                        if (allowedCandidateIndex < 0) {
                            allowedCandidateIndex = index;
                        }
                        if (!isVisible(candidate)) {
                            hiddenAllowedCount += 1;
                            continue;
                        }
                        return {
                            detected: true,
                            clicked: false,
                            strategy: `dom:contact_icon:${matchedIconKind}`,
                            clickMode: "playwright_locator",
                            candidateIndex: index,
                            matchedIconClass: matchedIconKind,
                            candidate_count: allowedCandidateCount,
                            candidate_summary: candidateSummary,
                        };
                    }
                    if (hiddenAllowedCount > 0) {
                        return {
                            detected: true,
                            clicked: false,
                            strategy: "dom:contact_icon:not_visible",
                            clickMode: "dom_scan",
                            candidateIndex: allowedCandidateIndex,
                            matchedIconClass: "",
                            candidate_count: allowedCandidateCount,
                            candidate_summary: candidateSummary,
                        };
                    }
                    return {
                        detected: false,
                        clicked: false,
                        strategy: candidates.length > 0
                            ? "dom:contact_icon:not_allowed"
                            : "dom:contact_icon:not_found",
                        clickMode: "dom_scan",
                        candidateIndex: allowedCandidateIndex,
                        matchedIconClass: "",
                        candidate_count: candidates.length,
                        candidate_summary: candidateSummary,
                    };
                }
                """,
                {
                    "rootSelector": CONTACT_ICON_ROOT_SELECTOR,
                    "buttonSelector": CONTACT_ICON_BUTTON_SELECTOR,
                    "buttonItemSelector": CONTACT_ICON_BUTTON_ITEM_SELECTOR,
                    "allowedIconClasses": list(ALLOWED_CONTACT_ICON_CLASSES),
                },
            )
        except PlaywrightError:
            return {"detected": False, "clicked": False, "strategy": "dom:error"}
        if not isinstance(result, dict):
            return {"detected": False, "clicked": False, "strategy": "dom:invalid"}
        raw_candidate_index = result.get("candidateIndex", -1)
        candidate_index = int(raw_candidate_index) if raw_candidate_index is not None else -1
        clicked = False
        click_mode = str(result.get("clickMode") or "")
        click_error = ""
        page_count_before = 0
        page_count_after = 0
        unexpected_new_page = False
        page_guard_breach_reason = ""
        matched_icon_class = str(result.get("matchedIconClass") or "")
        if candidate_index >= 0 and bool(result.get("detected")):
            click_result = self._click_contact_badge_candidate(candidate_index=candidate_index)
            clicked = bool(click_result.get("clicked"))
            if clicked:
                click_mode = "playwright_locator"
            click_error = str(click_result.get("click_error") or "")
            page_count_before = int(click_result.get("page_count_before") or 0)
            page_count_after = int(click_result.get("page_count_after") or 0)
            unexpected_new_page = bool(click_result.get("unexpected_new_page"))
            page_guard_breach_reason = str(click_result.get("page_guard_breach_reason") or "")
            if unexpected_new_page:
                self._contact_badge_click_failure_reason = "unexpected_new_page"
                result["strategy"] = "dom:contact_icon:unexpected_new_page"
        return {
            "detected": bool(result.get("detected")),
            "clicked": clicked,
            "strategy": str(result.get("strategy") or ""),
            "click_mode": click_mode,
            "candidate_index": candidate_index,
            "matched_icon_class": matched_icon_class,
            "candidate_count": int(result.get("candidate_count") or 0),
            "candidate_summary": str(result.get("candidate_summary") or ""),
            "page_count_before": page_count_before,
            "page_count_after": page_count_after,
            "unexpected_new_page": unexpected_new_page,
            "page_guard_breach_reason": page_guard_breach_reason,
            "click_error": click_error,
        }

    def _click_contact_badge_candidate(self, *, candidate_index: int) -> dict[str, object]:
        if self._page is None:
            return {"clicked": False, "click_error": "no_page"}
        pages_before = self._alive_context_pages()
        page_count_before = len(pages_before)
        guard_breached_before = bool(getattr(self, "_page_guard_breached", False))
        guard_reason_before = str(getattr(self, "_page_guard_breach_reason", "") or "")
        try:
            root = self._page.locator(CONTACT_ICON_ROOT_SELECTOR).first
            buttons = root.locator(CONTACT_ICON_BUTTON_SELECTOR).first.locator(
                CONTACT_ICON_BUTTON_ITEM_SELECTOR
            )
            count = buttons.count()
            if candidate_index < 0 or candidate_index >= count:
                return {
                    "clicked": False,
                    "click_error": "candidate_index_out_of_range",
                    "page_count_before": page_count_before,
                    "page_count_after": page_count_before,
                    "unexpected_new_page": False,
                    "page_guard_breach_reason": guard_reason_before,
                }
            buttons.nth(candidate_index).click(timeout=1500)
        except PlaywrightError:
            return {
                "clicked": False,
                "click_error": "playwright_click_error",
                "page_count_before": page_count_before,
                "page_count_after": len(self._alive_context_pages()),
                "unexpected_new_page": False,
                "page_guard_breach_reason": str(
                    getattr(self, "_page_guard_breach_reason", "") or ""
                ),
            }
        self._wait_for_post_click_settle()
        pages_after = self._alive_context_pages()
        page_count_after = len(pages_after)
        guard_reason_after = str(getattr(self, "_page_guard_breach_reason", "") or "")
        unexpected_pages = [
            page for page in pages_after if page is not self._page and page not in pages_before
        ]
        unexpected_new_page = page_count_after > page_count_before or (
            guard_reason_after in UNEXPECTED_TAB_BREACH_REASONS
            and (not guard_breached_before or guard_reason_after != guard_reason_before)
        )
        unexpected_page_urls = [self._page_url(page) for page in unexpected_pages]
        for page in unexpected_pages:
            try:
                page.close()
            except PlaywrightError:
                continue
        if unexpected_new_page:
            self._mark_page_guard_breached("unexpected_tab_after_contact_click")
            self._log_event(
                "unexpected_tab_after_contact_click",
                task_index=self._current_task_index,
                page_count_before=page_count_before,
                page_count_after=page_count_after,
                unexpected_page_urls=unexpected_page_urls,
                page_guard_breach_reason=guard_reason_after,
            )
            return {
                "clicked": False,
                "click_error": "unexpected_new_page",
                "page_count_before": page_count_before,
                "page_count_after": page_count_after,
                "unexpected_new_page": True,
                "page_guard_breach_reason": guard_reason_after,
            }
        return {
            "clicked": True,
            "click_error": "",
            "page_count_before": page_count_before,
            "page_count_after": page_count_after,
            "unexpected_new_page": False,
            "page_guard_breach_reason": guard_reason_after,
        }

    def _alive_context_pages(self) -> list[object]:
        context = getattr(self, "_context", None)
        if context is None:
            return []
        try:
            pages = list(context.pages)
        except PlaywrightError:
            return []
        alive_pages: list[object] = []
        for page in pages:
            if self._page_url(page):
                alive_pages.append(page)
        return alive_pages

    def _page_url(self, page) -> str:
        if page is None:
            return ""
        try:
            return str(page.url or "")
        except PlaywrightError:
            return ""

    def _wait_for_post_click_settle(self) -> None:
        deadline = monotonic() + POST_CLICK_SETTLE_SECONDS
        while monotonic() < deadline:
            if str(getattr(self, "_page_guard_breach_reason", "") or "") in (
                UNEXPECTED_TAB_BREACH_REASONS
            ):
                break
            sleep(0.05)

    def _maybe_capture_profile_from_dom(self) -> bool:
        if self._page is None or self._current_task_index is None:
            return False
        if not self._current_page_matches_current_item():
            return False
        now = datetime.now(UTC)
        if (
            self._last_dom_profile_probe_at is not None
            and (now - self._last_dom_profile_probe_at).total_seconds() < 1
        ):
            return False
        self._last_dom_profile_probe_at = now
        item = self._items_by_index.get(self._current_task_index)
        if item is None:
            return False
        creator_id = normalized_creator_id(item.task_data.get("creator_oecuid"))
        if not creator_id:
            return False
        dom_profile = self._extract_profile_dom_snapshot()
        if not dom_profile.get("ready"):
            return False
        bio = str(dom_profile.get("bio") or "").strip()
        if not bio:
            return False
        has_action = bool(dom_profile.get("has_action"))
        if not has_action:
            return False
        action_strategy = str(dom_profile.get("action_strategy") or "")
        signature = f"{self._current_task_index}:{creator_id}:{bio}:{action_strategy}"
        if (
            signature == self._last_dom_profile_signature
            and self._last_dom_profile_seen_at is not None
            and (now - self._last_dom_profile_seen_at).total_seconds() < 1
        ):
            return False
        self._last_dom_profile_signature = signature
        self._last_dom_profile_seen_at = now
        if action_strategy:
            self._last_contact_badge_strategy = action_strategy
        self._last_contact_badge_detected = True
        self._contact_positive_signal = True
        self._log_event(
            "profile_dom_ready",
            task_index=self._current_task_index,
            bio_length=len(bio),
            badge_strategy=action_strategy,
        )
        self._store_profile_capture(
            task_index=self._current_task_index,
            creator_id=creator_id,
            payload={
                "code": 0,
                "creator_profile": {
                    "creator_oecuid": creator_id,
                    "bio": bio,
                    "contact_info_available": {
                        "value": True,
                        "is_authorized": True,
                        "status": 0,
                    },
                },
            },
            shop_region=self._current_region or "",
            profile_types=(1,),
            page_url=self._safe_page_url(),
        )
        return True

    def _extract_profile_dom_snapshot(self) -> dict[str, object]:
        if self._page is None:
            return {"ready": False, "bio": "", "has_action": False, "action_strategy": ""}
        try:
            result = self._page.evaluate(
                """
                ({ rootSelector, buttonSelector, allowedIconClasses }) => {
                    const root = document.querySelector(rootSelector);
                    if (!(root instanceof HTMLElement)) {
                        return {
                            ready: false,
                            bio: "",
                            hasAction: false,
                            actionStrategy: "profile_dom:no_root",
                        };
                    }
                    const isVisible = (element) => {
                        if (!(element instanceof HTMLElement)) {
                            return false;
                        }
                        const style = window.getComputedStyle(element);
                        if (style.display === "none" || style.visibility === "hidden") {
                            return false;
                        }
                        const rect = element.getBoundingClientRect();
                        return rect.width > 0 && rect.height > 0;
                    };
                    const classTokens = (element) => {
                        if (!(element instanceof Element)) {
                            return [];
                        }
                        const rawClassName =
                            typeof element.className === "string"
                                ? element.className
                                : element.className?.baseVal || "";
                        return rawClassName
                            .split(/\\s+/)
                            .map((token) => token.trim().toLowerCase())
                            .filter(Boolean);
                    };
                    const iconKind = (tokens) => {
                        for (const token of tokens) {
                            if (allowedIconClasses.includes(token)) {
                                return token;
                            }
                        }
                        return "";
                    };
                    const describeTarget = (target, iconTokens) => {
                        if (!(target instanceof HTMLElement)) {
                            return "";
                        }
                        const className =
                            typeof target.className === "string" ? target.className.trim() : "";
                        return [
                            target.tagName.toLowerCase() || "",
                            target.getAttribute("data-e2e") || "",
                            target.getAttribute("data-tid") || "",
                            target.getAttribute("aria-label") || "",
                            target.getAttribute("title") || "",
                            className,
                            iconTokens.join(","),
                        ]
                            .map((part) =>
                                typeof part === "string" ? part.trim().toLowerCase() : "",
                            )
                            .filter(Boolean)
                            .join("|");
                    };
                    const bioSelectors = [
                        "#creator-detail-profile-container [data-e2e='2e9732e6-4d06-458d']",
                        "#creator-detail-profile-container .whitespace-pre-wrap",
                        "#creator-detail-profile-container [class*='break-words']",
                    ];
                    let bio = "";
                    for (const selector of bioSelectors) {
                        const node = document.querySelector(selector);
                        if (!(node instanceof HTMLElement) || !isVisible(node)) {
                            continue;
                        }
                        const text = (node.innerText || node.textContent || "").trim();
                        if (text.length > bio.length) {
                            bio = text;
                        }
                    }
                    let hasAction = false;
                    let actionStrategy = "";
                    const candidates = Array.from(root.querySelectorAll(buttonSelector)).filter(
                        (node) => node instanceof HTMLElement,
                    );
                    for (const candidate of candidates) {
                        if (!(candidate instanceof HTMLElement) || !isVisible(candidate)) {
                            continue;
                        }
                        const svg = candidate.querySelector("svg");
                        const iconTokens = classTokens(svg);
                        const matchedIconKind = iconKind(iconTokens);
                        if (!matchedIconKind) {
                            continue;
                        }
                        hasAction = true;
                        actionStrategy = `profile_dom:contact_icon:${matchedIconKind}:${
                            describeTarget(candidate, iconTokens)
                        }`;
                        if (hasAction) {
                            break;
                        }
                    }
                    return {
                        ready: Boolean(bio) || hasAction,
                        bio,
                        hasAction,
                        actionStrategy,
                    };
                }
                """,
                {
                    "rootSelector": CONTACT_ICON_ROOT_SELECTOR,
                    "buttonSelector": CONTACT_ICON_BUTTON_SELECTOR,
                    "allowedIconClasses": list(ALLOWED_CONTACT_ICON_CLASSES),
                },
            )
        except PlaywrightError:
            return {"ready": False, "bio": "", "has_action": False, "action_strategy": ""}
        if not isinstance(result, dict):
            return {"ready": False, "bio": "", "has_action": False, "action_strategy": ""}
        return {
            "ready": bool(result.get("ready")),
            "bio": str(result.get("bio") or ""),
            "has_action": bool(result.get("hasAction")),
            "action_strategy": str(result.get("actionStrategy") or ""),
        }
