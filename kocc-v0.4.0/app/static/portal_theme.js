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
    window.KOCCTheme = {apply, current:() => normalize(localStorage.getItem(key))};
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
    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", initializeAccountMenu, {once:true});
    } else {
        initializeAccountMenu();
    }
})();
