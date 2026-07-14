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
