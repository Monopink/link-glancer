from __future__ import annotations

import json

from creator_enrichment.constants import CONTACT_ICON_CLASS_KEYWORDS


def network_capture_init_script() -> str:
    keywords_json = json.dumps(list(CONTACT_ICON_CLASS_KEYWORDS))
    return f"""
(() => {{
    if (window.__linkGlancerCaptureInstalled) {{
        return;
    }}
    window.__linkGlancerCaptureInstalled = true;
    window.__linkGlancerCapture = {{
        profileResponses: [],
        contactResponses: [],
        badgeEvents: [],
        badgeClickPageUrl: "",
    }};
    const pushCapture = (kind, entry) => {{
        const list = window.__linkGlancerCapture[kind];
        if (!Array.isArray(list)) {{
            return;
        }}
        list.push(entry);
        if (list.length > 20) {{
            list.splice(0, list.length - 20);
        }}
    }};
    const parseJsonText = (text) => {{
        if (typeof text !== "string" || !text.trim()) {{
            return null;
        }}
        try {{
            return JSON.parse(text);
        }} catch {{
            return null;
        }}
    }};
    const parseProfileTypes = (body) => {{
        const payload = parseJsonText(body);
        if (!payload || !Array.isArray(payload.profile_types)) {{
            return [];
        }}
        return payload.profile_types;
    }};
    const contactBadgeKeywords = {keywords_json};
    const isVisible = (element) => {{
        if (!(element instanceof HTMLElement)) {{
            return false;
        }}
        const style = window.getComputedStyle(element);
        if (style.display === "none" || style.visibility === "hidden") {{
            return false;
        }}
        const rect = element.getBoundingClientRect();
        return rect.width > 0 && rect.height > 0;
    }};
    const resolveClickable = (element, container) => {{
        let current = element;
        while (current instanceof HTMLElement && current !== container) {{
            const tagName = current.tagName.toLowerCase();
            if (
                tagName === "button"
                || tagName === "a"
                || current.classList.contains("cursor-pointer")
                || current.getAttribute("role") === "button"
                || typeof current.onclick === "function"
            ) {{
                return current;
            }}
            current = current.parentElement;
        }}
        return element instanceof HTMLElement ? element : null;
    }};
    const reportBadge = (entry) => {{
        pushCapture("badgeEvents", {{
            pageUrl: location.href,
            detected: Boolean(entry && entry.detected),
            clicked: Boolean(entry && entry.clicked),
            strategy: typeof entry?.strategy === "string" ? entry.strategy : "",
        }});
    }};
    const monitorContactBadge = () => {{
        const root = window.__linkGlancerCapture;
        const container = document.querySelector("#creator-detail-profile-container");
        if (!(container instanceof HTMLElement)) {{
            return;
        }}
        if (root.badgeClickPageUrl === location.href) {{
            return;
        }}
        const icons = Array.from(
            container.querySelectorAll("div.cursor-pointer svg[class*='alliance-icon-']"),
        );
        for (const icon of icons) {{
            if (!(icon instanceof SVGElement)) {{
                continue;
            }}
            const className = icon.getAttribute("class") || "";
            const matchedKeyword = contactBadgeKeywords.find(
                (keyword) => className.includes(keyword),
            );
            if (!matchedKeyword) {{
                continue;
            }}
            const target = resolveClickable(icon.parentElement || icon, container);
            if (!(target instanceof HTMLElement) || !isVisible(target)) {{
                reportBadge({{
                    detected: true,
                    clicked: false,
                    strategy: `observer:${{matchedKeyword}}:not_visible`,
                }});
                return;
            }}
            try {{
                root.badgeClickPageUrl = location.href;
                target.click();
                reportBadge({{
                    detected: true,
                    clicked: true,
                    strategy: `observer:${{matchedKeyword}}:click`,
                }});
            }} catch {{
                root.badgeClickPageUrl = "";
                reportBadge({{
                    detected: true,
                    clicked: false,
                    strategy: `observer:${{matchedKeyword}}:visible`,
                }});
            }}
            return;
        }}
    }};
    const handlePayload = (url, pageUrl, payload, bodyText) => {{
        if (!payload || typeof payload !== "object") {{
            return;
        }}
        let pathname = "";
        try {{
            pathname = new URL(url, location.href).pathname;
        }} catch {{
            pathname = "";
        }}
        if (pathname === "/api/v1/oec/affiliate/creator/marketplace/profile") {{
            pushCapture("profileResponses", {{
                url,
                pageUrl,
                payload,
                profileTypes: parseProfileTypes(bodyText),
            }});
            return;
        }}
        if (pathname === "/api_sens/v1/affiliate/cmp/contact") {{
            pushCapture("contactResponses", {{
                url,
                pageUrl,
                payload,
                creatorId: new URL(url, location.href).searchParams.get("creator_oecuid") || "",
            }});
        }}
    }};

    const originalFetch = window.fetch.bind(window);
    window.fetch = async (...args) => {{
        const request = args[0];
        const init = args[1];
        let url = "";
        let bodyText = "";
        try {{
            if (typeof request === "string") {{
                url = request;
            }} else if (request && typeof request.url === "string") {{
                url = request.url;
            }}
            if (init && typeof init.body === "string") {{
                bodyText = init.body;
            }} else if (request && typeof request.clone === "function") {{
                const clonedRequest = request.clone();
                bodyText = await clonedRequest.text();
            }}
        }} catch {{}}
        const response = await originalFetch(...args);
        try {{
            const clonedResponse = response.clone();
            const payload = await clonedResponse.json();
            handlePayload(response.url || url, location.href, payload, bodyText);
        }} catch {{}}
        return response;
    }};

    const originalOpen = XMLHttpRequest.prototype.open;
    const originalSend = XMLHttpRequest.prototype.send;
    XMLHttpRequest.prototype.open = function(method, url, ...rest) {{
        this.__linkGlancerUrl = typeof url === "string" ? url : "";
        return originalOpen.call(this, method, url, ...rest);
    }};
    XMLHttpRequest.prototype.send = function(body) {{
        this.__linkGlancerBodyText = typeof body === "string" ? body : "";
        this.addEventListener("loadend", () => {{
            try {{
                const payload = parseJsonText(this.responseText);
                handlePayload(
                    this.responseURL || this.__linkGlancerUrl || "",
                    location.href,
                    payload,
                    this.__linkGlancerBodyText || "",
                );
            }} catch {{}}
        }});
        return originalSend.call(this, body);
    }};
    monitorContactBadge();
    const badgeObserver = new MutationObserver(() => {{
        monitorContactBadge();
    }});
    badgeObserver.observe(document.documentElement, {{
        childList: true,
        subtree: true,
        attributes: true,
    }});
}})();
"""


def enrichment_collection_mode_script() -> str:
    return """
() => {
    const installedKey = "__linkGlancerEnrichmentModeInstalled";
    if (window[installedKey]) {
        return;
    }
    window[installedKey] = true;

    const style = document.createElement("style");
    style.id = "link-glancer-enrichment-mode";
    style.textContent = `
        #im-entry,
        aside,
        [data-tid="m4b_page_header"],
        .entryWrapper-Zd4AJg,
        .core-modal-wrapper,
        .core-modal-mask,
        [data-modal-root="true"],
        [id^="video_preview_"],
        #video-roll-root-mask,
        #feedback-container,
        .lightcharts-tooltip,
        .ad-box,
        tiktok-cookie-banner,
        [data-e2e="f03cd1ad-3b77-b803"],
        [data-e2e="3bff47b5-27c3-70bd"],
        [class*="similar-creator-card__Container"],
        [class*="similar-creator-card__SaveContainer"] {
            display: none !important;
        }
        #content-container,
        main,
        #affiliate_sub_app_container,
        #modern_sub_app_container_connection {
            max-width: none !important;
            width: 100% !important;
        }
        #scroll-container,
        main,
        #affiliate_sub_app_container {
            overflow-anchor: none !important;
        }
        [data-e2e="e4af3b5a-a87c-dfbd"],
        [data-e2e="1a71a34c-27e4-2557"],
        [data-e2e="807bb724-4218-1bc0"],
        #sales_tab,
        #collab_history,
        #video_tab,
        #live_tab,
        #followers_tab,
        #trends_tab,
        #top_video,
        section[data-pre],
        .pulse-tabs,
        [data-tid="m4b_tabs"] {
            display: none !important;
        }
        *,
        *::before,
        *::after {
            animation: none !important;
            transition: none !important;
            scroll-behavior: auto !important;
        }
        img,
        video,
        canvas {
            visibility: hidden !important;
        }
    `;
    document.head.appendChild(style);

    const keepNodes = new Set();
    const updateKeepNodes = () => {
        keepNodes.clear();
        const profile = document.querySelector("#creator-detail-profile-container");
        if (!(profile instanceof HTMLElement)) {
            return false;
        }
        keepNodes.add(profile);
        let current = profile.parentElement;
        while (current instanceof HTMLElement) {
            keepNodes.add(current);
            current = current.parentElement;
        }
        return true;
    };

    const shouldRemove = (node) => {
        if (!(node instanceof HTMLElement)) {
            return false;
        }
        if (
            node.matches(
                ".core-modal-wrapper, .core-modal-mask, [data-modal-root='true'], "
                + "[id^='video_preview_'], #video-roll-root-mask, #feedback-container, "
                + ".lightcharts-tooltip, .ad-box, tiktok-cookie-banner, "
                + "[data-e2e='e4af3b5a-a87c-dfbd'], [data-e2e='1a71a34c-27e4-2557'], "
                + "[data-e2e='807bb724-4218-1bc0'], [data-e2e='f03cd1ad-3b77-b803'], "
                + "[data-e2e='3bff47b5-27c3-70bd'], [class*='similar-creator-card__Container'], "
                + "[class*='similar-creator-card__SaveContainer'], #sales_tab, #collab_history, "
                + "#video_tab, #live_tab, #followers_tab, #trends_tab, #top_video, "
                + "section[data-pre], .pulse-tabs, [data-tid='m4b_tabs']"
            )
        ) {
            return true;
        }
        for (const keptNode of keepNodes) {
            if (keptNode === node || keptNode.contains(node) || node.contains(keptNode)) {
                return false;
            }
        }
        return (
            node.closest("#creator-detail-profile-container") === null
            && !node.querySelector("#creator-detail-profile-container")
        );
    };

    const pruneNode = (node) => {
        if (!(node instanceof HTMLElement)) {
            return;
        }
        if (shouldRemove(node)) {
            node.remove();
            return;
        }
        for (const img of node.querySelectorAll("img")) {
            img.removeAttribute("src");
            img.removeAttribute("srcset");
            img.loading = "lazy";
        }
        for (const media of node.querySelectorAll("video, source")) {
            media.remove();
        }
        for (const preview of node.querySelectorAll("[id^='video_preview_']")) {
            preview.remove();
        }
    };

    if (updateKeepNodes()) {
        pruneNode(document.body);
    }
    const observer = new MutationObserver((mutations) => {
        const ready = updateKeepNodes();
        if (!ready) {
            return;
        }
        for (const mutation of mutations) {
            for (const addedNode of mutation.addedNodes) {
                pruneNode(addedNode);
            }
        }
    });
    observer.observe(document.documentElement, {
        childList: true,
        subtree: true,
    });
    window.__linkGlancerEnrichmentModeObserver = observer;
}
"""
