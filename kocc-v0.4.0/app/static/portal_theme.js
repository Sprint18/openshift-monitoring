(() => {
    "use strict";
    const key = "dashboardTheme";
    const root = document.documentElement;
    const normalize = theme => theme === "dark" ? "dark" : "light";
    const apply = theme => {
        const selected = normalize(theme);
        root.dataset.theme = selected;
        localStorage.setItem(key, selected);
        window.dispatchEvent(new CustomEvent("kocc:themechange", {detail:{theme:selected}}));
        return selected;
    };
    const statusClass = value => {
        const status = String(value || "").toLowerCase().replace(/[^a-z0-9]/g, "");
        if (["healthy","ready","assigned","available","online","recovered","targetreached"].includes(status)) return "status-success";
        if (["pending","podinitializing","containercreating","notready","progressing","stale","mixedversion","oldversion","notupdated","unknownversion","partlyunknown"].includes(status)) return "status-warning";
        if (["crashloopbackoff","imagepullbackoff","errimagepull","error","failed","critical","unavailable","degraded","regression","persistingerror","newwitherrors"].includes(status)) return "status-danger";
        if (["running","newresource","improving"].includes(status)) return "status-info";
        return "status-neutral";
    };
    window.KOCCTheme = {apply, statusClass, current:() => normalize(localStorage.getItem(key))};
    apply(localStorage.getItem(key));

    const initializeAccountMenu = () => {
        const menu = document.querySelector(".account-menu");
        if (!menu || menu.dataset.initialized === "true") return;
        menu.dataset.initialized = "true";
        const summary = menu.querySelector("summary");
        const dropdown = menu.querySelector(".account-dropdown");
        const position = () => {
            if (!menu.open) return;
            const anchor = summary.getBoundingClientRect();
            dropdown.style.top = `${anchor.bottom + 5}px`;
            dropdown.style.right = `${Math.max(12, window.innerWidth - anchor.right)}px`;
        };
        menu.addEventListener("toggle", position);
        window.addEventListener("resize", position);
        document.addEventListener("click", event => {
            if (menu.open && !menu.contains(event.target)) menu.removeAttribute("open");
        });
        document.addEventListener("keydown", event => {
            if (event.key === "Escape" && menu.open) {
                menu.removeAttribute("open");
                summary.focus();
            }
        });
    };
    const initializeSessionActivity = () => {
        if (!document.querySelector(".account-menu")) return;
        const minimumInterval = 60000;
        let lastSent = 0;
        let pending = false;
        const report = () => {
            const now = Date.now();
            if (pending || now - lastSent < minimumInterval) return;
            pending = true;
            lastSent = now;
            fetch("/api/session/activity", {
                method: "POST",
                headers: {"Accept": "application/json"},
                credentials: "same-origin",
            }).then(response => {
                if (response.status === 401) {
                    const next = encodeURIComponent(location.pathname + location.search);
                    location.assign(`/login?next=${next}`);
                }
            }).catch(() => {}).finally(() => { pending = false; });
        };
        document.addEventListener("click", report, {passive:true});
        document.addEventListener("keydown", report);
    };
    const initialize = () => {
        initializeAccountMenu();
        initializeSessionActivity();
    };
    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", initialize, {once:true});
    } else {
        initialize();
    }
})();
