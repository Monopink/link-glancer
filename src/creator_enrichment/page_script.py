from __future__ import annotations


def network_capture_init_script() -> str:
    return """
(() => {{
    if (window.__linkGlancerCaptureInstalled) {{
        return;
    }}
    window.__linkGlancerCaptureInstalled = true;
    window.__linkGlancerCapture = {{
        profileResponses: [],
        contactResponses: [],
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
        #content-container > aside,
        #content-container > main > #im-entry,
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
        #modern_sub_app_container_connection,
        #garfish_app_for_connection_6o7h3vss,
        [__garfishmockbody__] {
            max-width: none !important;
            width: 100% !important;
        }
        #scroll-container,
        main,
        #affiliate_sub_app_container,
        #content-container {
            overflow-anchor: none !important;
        }
        #content-container {
            display: block !important;
            padding-top: 72px !important;
            padding-left: 0 !important;
        }
        #content-container > main {
            width: 100% !important;
            margin: 0 !important;
            padding: 0 !important;
            flex: none !important;
        }
        #content-container > main > div,
        #affiliate_sub_app_container > div,
        #modern_sub_app_container_connection > div,
        [__garfishmockbody__] > div {
            margin: 0 !important;
            padding: 0 !important;
            width: 100% !important;
            max-width: none !important;
        }
        #scroll-container > .h-60.fixed {
            min-height: 60px !important;
            padding: 0 16px !important;
            background: rgba(23, 25, 29, 0.92) !important;
            backdrop-filter: blur(4px) !important;
        }
        #scroll-container > .h-60.fixed > :first-child,
        #scroll-container > .h-60.fixed #im-nav,
        #scroll-container > .h-60.fixed .pulse-badge,
        #scroll-container > .h-60.fixed [class*="Help"],
        #scroll-container > .h-60.fixed [class*="Help"] * {
            display: none !important;
        }
        #scroll-container > .h-60.fixed > :last-child {
            display: flex !important;
            width: 100% !important;
            justify-content: space-between !important;
            align-items: center !important;
            gap: 12px !important;
        }
        #scroll-container > .h-60.fixed > :last-child > div {
            display: flex !important;
            align-items: center !important;
            gap: 8px !important;
        }
        #scroll-container > .h-60.fixed #region-selector,
        #scroll-container > .h-60.fixed #region-selector * {
            display: initial !important;
        }
        #scroll-container > .h-60.fixed #region-selector {
            display: flex !important;
            pointer-events: auto !important;
            margin-left: auto !important;
        }
        #scroll-container > .h-60.fixed #region-selector > div,
        #scroll-container > .h-60.fixed #region-selector > div > div {
            display: inline-flex !important;
            align-items: center !important;
            cursor: pointer !important;
        }
        #creator-detail-profile-container {
            align-items: flex-start !important;
            gap: 20px !important;
        }
        #creator-detail-profile-container > [data-e2e="1d25e140-5f50-b605"] {
            flex: 1 1 auto !important;
            min-width: 0 !important;
        }
        #creator-detail-profile-container > [data-e2e="f1003596-3f9a-0aeb"] {
            flex: 0 0 auto !important;
            align-self: flex-start !important;
            position: static !important;
            min-width: max-content !important;
            padding-left: 12px !important;
            background: transparent !important;
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
