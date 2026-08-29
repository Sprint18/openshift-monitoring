(() => {
    "use strict";

    const root = document.getElementById("shiftlight-root");
    if (!root) return;

    const SESSION_KEY = "kocc.shiftlight.conversations.v1";
    const NUDGE_KEY = "kocc.shiftlight.nudge-dismissed.v1";
    const STORE_VERSION = 2;
    const MAX_CONVERSATIONS = 10;
    const MAX_MESSAGES = 100;
    const MAX_TEXT_LENGTH = 50000;
    const FACT_KEYS = Object.freeze({
        resource_count: "Toplam kaynak",
        degraded_true_count: "Degraded",
        available_false_count: "Unavailable",
        progressing_true_count: "Progressing"
    });
    const MESSAGES = Object.freeze({
        request: "İstek ShiftLight AI tarafından işlenemedi.",
        unavailable: "ShiftLight AI şu anda kullanılamıyor. Lütfen daha sonra tekrar deneyin.",
        timeout: "ShiftLight AI yanıtı zaman aşımına uğradı. Lütfen tekrar deneyin.",
        generic: "ShiftLight AI isteği tamamlanamadı."
    });
    const suggestions = [
        "Cluster sağlık durumunu kontrol et",
        "Degraded ClusterOperator var mı?",
        "Node CPU ve memory kullanımını incele",
        "Namespace pod durumlarını kontrol et"
    ];

    const launcher = document.getElementById("shiftlight-launcher");
    const flightMascot = document.getElementById("shiftlight-flight");
    const drawer = document.getElementById("shiftlight-drawer");
    const overlay = document.getElementById("shiftlight-overlay");
    const closeButton = document.getElementById("shiftlight-close");
    const expandButton = document.getElementById("shiftlight-expand");
    const nudge = document.getElementById("shiftlight-nudge");
    const nudgeClose = document.getElementById("shiftlight-nudge-close");
    const historyToggle = document.getElementById("shiftlight-history-toggle");
    const historyPanel = document.getElementById("shiftlight-history");
    const historyClose = document.getElementById("shiftlight-history-close");
    const historyList = document.getElementById("shiftlight-history-list");
    const newButton = document.getElementById("shiftlight-new");
    const conversation = document.getElementById("shiftlight-conversation");
    const form = document.getElementById("shiftlight-form");
    const messageInput = document.getElementById("shiftlight-message");
    const sendButton = document.getElementById("shiftlight-send");
    const statusText = document.getElementById("shiftlight-status");
    let requestPending = false;
    let fullscreen = false;
    let welcomeRun = 0;

    const setFlightState = (state) => { flightMascot.dataset.state = state; };

    class ShiftLightUIError {
        constructor(kind) { this.kind = kind; }
    }

    const conversationId = () => globalThis.crypto && typeof globalThis.crypto.randomUUID === "function" ? globalThis.crypto.randomUUID() : `${Date.now()}-${Math.random().toString(16).slice(2)}`;
    const nowIso = () => new Date().toISOString();
    const emptyConversation = () => { const timestamp = nowIso(); return {id: conversationId(), createdAt: timestamp, updatedAt: timestamp, messages: []}; };
    const emptyStore = () => ({version: STORE_VERSION, activeConversationId: null, conversations: []});
    const isSafeInteger = (value) => Number.isInteger(value) && value >= 0;
    const safeFacts = (facts) => {
        if (!facts || typeof facts !== "object" || Array.isArray(facts)) return {};
        const result = {};
        Object.keys(FACT_KEYS).forEach((key) => {
            if (isSafeInteger(facts[key])) result[key] = facts[key];
        });
        return result;
    };
    const safeEvidence = (items) => {
        if (!Array.isArray(items)) return [];
        return items.flatMap((item) => {
            if (!item || typeof item !== "object" || typeof item.tool !== "string" || item.status !== "success") return [];
            const safe = {tool: item.tool.slice(0, 100), status: "success", facts: safeFacts(item.facts)};
            if (["kkbtest", "rmtest"].includes(item.cluster)) safe.cluster = item.cluster;
            return [safe];
        }).slice(0, 20);
    };
    const safeMessage = (item) => {
        if (!item || typeof item !== "object" || !["user", "assistant"].includes(item.role) || typeof item.text !== "string") return null;
        const text = item.text.slice(0, MAX_TEXT_LENGTH);
        if (!text.trim()) return null;
        return {role: item.role, text, evidence: item.role === "assistant" ? safeEvidence(item.evidence) : []};
    };
    const safeConversation = (value) => {
        if (!value || typeof value !== "object") return emptyConversation();
        const messages = Array.isArray(value.messages) ? value.messages.map(safeMessage).filter(Boolean).slice(-MAX_MESSAGES) : [];
        const createdAt = typeof value.createdAt === "string" && !Number.isNaN(Date.parse(value.createdAt)) ? value.createdAt : nowIso();
        const updatedAt = typeof value.updatedAt === "string" && !Number.isNaN(Date.parse(value.updatedAt)) ? value.updatedAt : createdAt;
        return {id: typeof value.id === "string" && value.id ? value.id.slice(0, 100) : conversationId(), createdAt, updatedAt, messages};
    };
    const safeStore = (value) => {
        let conversations = [];
        let activeConversationId = null;
        if (value && [1, STORE_VERSION].includes(value.version) && Array.isArray(value.conversations)) {
            conversations = value.conversations.map(safeConversation).slice(-MAX_CONVERSATIONS);
            activeConversationId = typeof value.activeConversationId === "string" ? value.activeConversationId : null;
        } else if (value && (value.current || value.previous)) {
            conversations = [value.previous, value.current].filter(Boolean).map(safeConversation).slice(-MAX_CONVERSATIONS);
            activeConversationId = conversations.length ? conversations[conversations.length - 1].id : null;
        }
        if (!conversations.some((item) => item.id === activeConversationId)) activeConversationId = conversations.length ? conversations[conversations.length - 1].id : null;
        return {version: STORE_VERSION, activeConversationId, conversations};
    };
    const loadStore = () => {
        try { return safeStore(JSON.parse(sessionStorage.getItem(SESSION_KEY) || "null")); }
        catch (_error) { return emptyStore(); }
    };
    let store = loadStore();
    const persistStore = () => {
        store = safeStore(store);
        sessionStorage.setItem(SESSION_KEY, JSON.stringify(store));
    };
    const activeConversation = () => store.conversations.find((item) => item.id === store.activeConversationId) || null;
    const isHistoricalConversation = () => Boolean(store.conversations.length && store.activeConversationId !== store.conversations[store.conversations.length - 1].id);
    const createConversation = () => {
        const item = emptyConversation();
        store.conversations.push(item);
        store.conversations = store.conversations.slice(-MAX_CONVERSATIONS);
        store.activeConversationId = item.id;
        persistStore();
        return item;
    };

    const addText = (parent, tag, className, text) => {
        const element = document.createElement(tag);
        element.className = className;
        element.textContent = text;
        parent.appendChild(element);
        return element;
    };
    const appendInline = (parent, text) => {
        const token = /(`[^`\n]+`|\*\*[^*\n]+\*\*|\*[^*\n]+\*|\[[^\]\n]+\]\([^\s)]+\))/g;
        let cursor = 0;
        for (const match of text.matchAll(token)) {
            parent.appendChild(document.createTextNode(text.slice(cursor, match.index)));
            const value = match[0];
            if (value.startsWith("`")) addText(parent, "code", "", value.slice(1, -1));
            else if (value.startsWith("**")) addText(parent, "strong", "", value.slice(2, -2));
            else if (value.startsWith("*")) addText(parent, "em", "", value.slice(1, -1));
            else {
                const parts = value.match(/^\[([^\]]+)\]\(([^)]+)\)$/);
                const url = parts && parts[2];
                if (parts && /^https?:\/\//i.test(url)) {
                    const link = addText(parent, "a", "", parts[1]);
                    link.href = url;
                    link.target = "_blank";
                    link.rel = "noopener noreferrer";
                } else parent.appendChild(document.createTextNode(value));
            }
            cursor = match.index + value.length;
        }
        parent.appendChild(document.createTextNode(text.slice(cursor)));
    };
    const markdownCells = (value) => value.trim().replace(/^\||\|$/g, "").split("|").map((item) => item.trim());
    const csvCell = (value) => {
        let safe = String(value).replace(/\r\n?/g, "\n");
        if (/^[\t ]*[=+\-@]/.test(safe)) safe = `'${safe}`;
        return /[",\n]/.test(safe) ? `"${safe.replace(/"/g, '""')}"` : safe;
    };
    const tableTextRows = (table) => [...table.rows].map((row) => [...row.cells].map((cell) => cell.textContent || ""));
    const csvFilename = () => {
        const date = new Date(), pad = (value) => String(value).padStart(2, "0");
        return `shiftlight-table-${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}-${pad(date.getHours())}${pad(date.getMinutes())}.csv`;
    };
    const downloadTableCsv = (table) => {
        const csv = tableTextRows(table).map((row) => row.map(csvCell).join(",")).join("\r\n");
        const url = URL.createObjectURL(new Blob(["\uFEFF", csv], {type: "text/csv;charset=utf-8"}));
        const link = document.createElement("a");
        link.href = url; link.download = csvFilename(); link.hidden = true; document.body.appendChild(link); link.click(); link.remove(); URL.revokeObjectURL(url);
    };
    const addTableActions = (wrapper, table) => {
        const actions = document.createElement("div"); actions.className = "shiftlight-table-actions";
        const download = addText(actions, "button", "", "CSV İndir"); download.type = "button"; download.title = "Tabloyu CSV olarak indir"; download.setAttribute("aria-label", "Tabloyu CSV olarak indir");
        download.addEventListener("click", () => downloadTableCsv(table)); wrapper.appendChild(actions);
    };
    const renderMarkdown = (target, markdown) => {
        if (typeof markdown !== "string" || !markdown.trim()) throw new Error("empty answer");
        const lines = markdown.replace(/\r\n?/g, "\n").split("\n");
        let index = 0;
        const paragraph = (parts) => {
            const item = document.createElement("p");
            parts.forEach((part, position) => {
                if (position) item.appendChild(document.createElement("br"));
                appendInline(item, part);
            });
            target.appendChild(item);
        };
        while (index < lines.length) {
            const line = lines[index];
            if (!line.trim()) { index += 1; continue; }
            if (line.startsWith("```")) {
                const content = [];
                index += 1;
                while (index < lines.length && !lines[index].startsWith("```")) { content.push(lines[index]); index += 1; }
                if (index < lines.length) index += 1;
                const pre = document.createElement("pre");
                pre.className = "shiftlight-code";
                addText(pre, "code", "", content.join("\n"));
                target.appendChild(pre);
                continue;
            }
            const heading = line.match(/^(#{1,3})\s+(.+)$/);
            if (heading) {
                const title = document.createElement(`h${heading[1].length}`);
                appendInline(title, heading[2]);
                target.appendChild(title);
                index += 1;
                continue;
            }
            if (line.includes("|") && index + 1 < lines.length && /^\s*\|?\s*:?-{3,}/.test(lines[index + 1])) {
                const headers = markdownCells(line), rows = [];
                index += 2;
                while (index < lines.length && lines[index].includes("|") && lines[index].trim()) { rows.push(markdownCells(lines[index])); index += 1; }
                const wrapper = document.createElement("div"), viewport = document.createElement("div"), table = document.createElement("table"), head = document.createElement("thead"), headRow = document.createElement("tr");
                wrapper.className = "shiftlight-table";
                viewport.className = "shiftlight-table-viewport";
                headers.forEach((value) => appendInline(addText(headRow, "th", "", ""), value));
                head.appendChild(headRow); table.appendChild(head);
                const body = document.createElement("tbody");
                rows.forEach((row) => { const tr = document.createElement("tr"); headers.forEach((_value, column) => appendInline(addText(tr, "td", "", ""), row[column] || "")); body.appendChild(tr); });
                table.appendChild(body); addTableActions(wrapper, table); viewport.appendChild(table); wrapper.appendChild(viewport); target.appendChild(wrapper);
                continue;
            }
            const listMatch = line.match(/^\s*(?:([-*+])|(\d+)\.)\s+(.+)$/);
            if (listMatch) {
                const tag = listMatch[2] ? "ol" : "ul", list = document.createElement(tag);
                while (index < lines.length) {
                    const row = lines[index].match(/^\s*(?:([-*+])|(\d+)\.)\s+(.+)$/);
                    if (!row || (row[2] ? "ol" : "ul") !== tag) break;
                    appendInline(addText(list, "li", "", ""), row[3]); index += 1;
                }
                target.appendChild(list); continue;
            }
            const parts = [line]; index += 1;
            while (index < lines.length && lines[index].trim() && !/^(#{1,3})\s|^```|^\s*(?:[-*+] |\d+\. )/.test(lines[index])) { parts.push(lines[index]); index += 1; }
            paragraph(parts);
        }
        if (!target.childNodes.length) throw new Error("renderer produced no content");
    };
    const renderAnswer = (target, text) => {
        try { renderMarkdown(target, text); }
        catch (_error) { target.replaceChildren(); target.textContent = text || MESSAGES.generic; }
    };
    const appendEvidence = (article, evidence) => {
        if (!evidence.length) return;
        const details = document.createElement("details");
        details.className = "shiftlight-evidence";
        addText(details, "summary", "", "Kullanılan cluster verileri");
        evidence.forEach((item) => {
            const list = document.createElement("dl");
            if (item.cluster) { addText(list, "dt", "", "Cluster"); addText(list, "dd", "", item.cluster.toUpperCase()); }
            addText(list, "dt", "", "Kaynak"); addText(list, "dd", "", item.tool);
            addText(list, "dt", "", "Durum"); addText(list, "dd", "", "Başarılı");
            Object.entries(item.facts).forEach(([key, value]) => {
                if (Object.hasOwn(FACT_KEYS, key)) { addText(list, "dt", "", FACT_KEYS[key]); addText(list, "dd", "", String(value)); }
            });
            details.appendChild(list);
        });
        article.appendChild(details);
    };
    const assistantShell = () => {
        const article = document.createElement("article"); article.className = "shiftlight-message assistant";
        const identity = document.createElement("div"); identity.className = "shiftlight-identity";
        const avatar = document.createElement("img"); avatar.className = "shiftlight-avatar"; avatar.src = "/static/shiftlight-mascot.png"; avatar.alt = ""; avatar.setAttribute("aria-hidden", "true"); identity.appendChild(avatar);
        addText(identity, "span", "", "ShiftLight AI"); article.appendChild(identity);
        const answer = document.createElement("div"); answer.className = "shiftlight-answer"; article.appendChild(answer);
        return {article, answer};
    };
    const addDetailedViewAction = (shell) => {
        if (!shell.answer.querySelector("table, pre")) return;
        shell.article.tabIndex = -1;
        const button = addText(shell.article.querySelector(".shiftlight-identity"), "button", "shiftlight-detail-action", "Detaylı Gör");
        button.type = "button"; button.title = "Yanıtı tam ekranda incele"; button.setAttribute("aria-label", "Yanıtı tam ekranda incele");
        button.addEventListener("click", () => openFullscreen(shell.article));
    };
    const renderEmpty = () => {
        const empty = document.createElement("div"); empty.className = "shiftlight-empty";
        const content = document.createElement("div"); const mascot = document.createElement("img"); mascot.className = "shiftlight-empty-mascot"; mascot.src = "/static/shiftlight-mascot.png"; mascot.alt = "KKB ShiftLight AI maskotu"; content.appendChild(mascot); addText(content, "h2", "", "KKB ShiftLight AI"); addText(content, "p", "", "OpenShift cluster'ınız hakkında nasıl yardımcı olabilirim?");
        const choices = document.createElement("div"); choices.className = "shiftlight-suggestions";
        suggestions.forEach((text) => { const button = addText(choices, "button", "", text); button.type = "button"; button.disabled = isHistoricalConversation(); button.addEventListener("click", () => { messageInput.value = text; messageInput.focus(); resizeComposer(); }); });
        content.appendChild(choices); empty.appendChild(content); conversation.appendChild(empty);
    };
    const conversationTitle = (item) => {
        const question = item.messages.find((message) => message.role === "user");
        if (!question) return "Yeni sohbet";
        return question.text.length > 36 ? `${question.text.slice(0, 36).trim()}…` : question.text;
    };
    const renderHistory = () => {
        historyList.replaceChildren();
        if (!store.conversations.length) { addText(historyList, "div", "shiftlight-history-empty", "Henüz sohbet yok."); return; }
        [...store.conversations].reverse().forEach((item) => {
            const button = document.createElement("button"); button.type = "button"; button.className = `shiftlight-history-item${item.id === store.activeConversationId ? " active" : ""}`;
            const time = addText(button, "time", "", new Date(item.updatedAt).toLocaleTimeString("tr-TR", {hour: "2-digit", minute: "2-digit"})); time.dateTime = item.updatedAt;
            addText(button, "strong", "", conversationTitle(item));
            button.addEventListener("click", () => { store.activeConversationId = item.id; persistStore(); closeHistory(); renderConversation(true); });
            historyList.appendChild(button);
        });
    };
    const closeHistory = () => { historyPanel.hidden = true; historyToggle.setAttribute("aria-expanded", "false"); };
    const toggleHistory = () => { const opening = historyPanel.hidden; historyPanel.hidden = !opening; historyToggle.setAttribute("aria-expanded", String(opening)); if (opening) renderHistory(); };
    const nearConversationBottom = () => conversation.scrollHeight - conversation.scrollTop - conversation.clientHeight < 80;
    const renderConversation = (forceBottom = false) => {
        const followBottom = forceBottom || nearConversationBottom();
        const previousScrollTop = conversation.scrollTop;
        conversation.replaceChildren();
        const selected = activeConversation();
        if (!selected || !selected.messages.length) renderEmpty();
        else selected.messages.forEach((item) => {
            if (item.role === "user") addText(conversation, "article", "shiftlight-message user", item.text);
            else { const shell = assistantShell(); renderAnswer(shell.answer, item.text); addDetailedViewAction(shell); appendEvidence(shell.article, item.evidence); conversation.appendChild(shell.article); }
        });
        form.hidden = isHistoricalConversation();
        renderHistory();
        requestAnimationFrame(() => {
            conversation.scrollTop = followBottom ? conversation.scrollHeight : previousScrollTop;
        });
    };

    const openDrawer = () => {
        cancelWelcome();
        drawer.classList.add("open"); drawer.setAttribute("aria-hidden", "false"); overlay.hidden = false;
        launcher.setAttribute("aria-expanded", "true"); messageInput.focus();
    };
    const setFullscreen = (expanded, focusTarget = null, restoreFocus = true) => {
        const scrollTop = conversation.scrollTop;
        fullscreen = expanded;
        drawer.classList.toggle("fullscreen", expanded); overlay.classList.toggle("fullscreen", expanded); document.body.classList.toggle("shiftlight-fullscreen-open", expanded);
        drawer.setAttribute("aria-modal", String(expanded)); expandButton.setAttribute("aria-pressed", String(expanded)); expandButton.textContent = expanded ? "↙" : "⛶";
        expandButton.title = expanded ? "Küçült" : "Tam Ekran"; expandButton.setAttribute("aria-label", expanded ? "ShiftLight'ı normal görünüme küçült" : "ShiftLight'ı tam ekran aç");
        requestAnimationFrame(() => { conversation.scrollTop = scrollTop; if (focusTarget) { focusTarget.scrollIntoView({block: "center"}); focusTarget.focus({preventScroll: true}); } else if (!expanded && restoreFocus) expandButton.focus(); });
    };
    const openFullscreen = (focusTarget = null) => { if (!drawer.classList.contains("open")) openDrawer(); setFullscreen(true, focusTarget); };
    const closeFullscreen = () => setFullscreen(false);
    const closeDrawer = () => {
        if (fullscreen) setFullscreen(false, null, false);
        drawer.classList.remove("open"); drawer.setAttribute("aria-hidden", "true"); overlay.hidden = true;
        launcher.setAttribute("aria-expanded", "false"); launcher.focus();
    };
    const resetWelcomeVisuals = () => {
        nudge.hidden = true; nudge.classList.remove("visible", "exiting");
        setFlightState("idle"); flightMascot.hidden = true;
        launcher.classList.remove("welcome-hidden");
    };
    const cancelWelcome = () => {
        welcomeRun += 1; sessionStorage.setItem(NUDGE_KEY, "true"); resetWelcomeVisuals();
    };
    const collapseWelcome = (run) => {
        if (run !== welcomeRun || flightMascot.hidden) return;
        nudge.classList.remove("visible"); nudge.classList.add("exiting"); launcher.classList.remove("welcome-hidden"); setFlightState("resting");
        window.setTimeout(() => { if (run === welcomeRun) resetWelcomeVisuals(); }, 420);
    };
    const revealNudge = (run) => {
        if (run !== welcomeRun) return;
        setFlightState("greeting"); nudge.hidden = false;
        requestAnimationFrame(() => nudge.classList.add("visible"));
        window.setTimeout(() => collapseWelcome(run), 6800);
    };
    const landMascot = (run, settle = 320) => {
        if (run !== welcomeRun) return;
        setFlightState("landed");
        window.setTimeout(() => revealNudge(run), settle);
    };
    const showNudge = (force = false) => {
        if (!force && (sessionStorage.getItem(NUDGE_KEY) === "true" || root.dataset.openOnLoad === "true")) { resetWelcomeVisuals(); return; }
        if (drawer.classList.contains("open") || fullscreen) { sessionStorage.setItem(NUDGE_KEY, "true"); resetWelcomeVisuals(); return; }
        const run = welcomeRun + 1; welcomeRun = run;
        sessionStorage.setItem(NUDGE_KEY, "true");
        const reducedMotion = Boolean(window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches);
        const mobile = Boolean(window.matchMedia && window.matchMedia("(max-width: 620px)").matches);
        resetWelcomeVisuals(); launcher.classList.add("welcome-hidden");
        window.setTimeout(() => {
            if (run !== welcomeRun) return;
            flightMascot.hidden = false; setFlightState("flight-ready");
            if (reducedMotion) { landMascot(run, 0); return; }
            requestAnimationFrame(() => requestAnimationFrame(() => {
                if (run === welcomeRun) setFlightState("flying");
            }));
        }, reducedMotion ? 120 : mobile ? 650 : 750);
    };
    window.shiftLightReplayWelcome = () => { sessionStorage.removeItem(NUDGE_KEY); showNudge(true); };
    const errorKind = (status) => status === 400 ? "request" : status === 502 || status === 503 ? "unavailable" : status === 504 ? "timeout" : "generic";
    const fetchJson = async (url, options) => {
        let response;
        try { response = await fetch(url, options); }
        catch (error) { throw new ShiftLightUIError(error && error.name === "AbortError" ? "timeout" : "unavailable"); }
        const type = (response.headers.get("Content-Type") || "").toLowerCase();
        let data = null;
        if (type.startsWith("application/json")) { try { data = await response.json(); } catch (_error) { data = null; } }
        if (!response.ok) throw new ShiftLightUIError(errorKind(response.status));
        if (data === null) throw new ShiftLightUIError("generic");
        return data;
    };
    const safeError = (error) => error instanceof ShiftLightUIError && MESSAGES[error.kind] ? MESSAGES[error.kind] : MESSAGES.unavailable;
    const resizeComposer = () => { messageInput.style.height = "auto"; messageInput.style.height = `${Math.min(messageInput.scrollHeight, 125)}px`; };
    const setPending = (pending) => {
        requestPending = pending; messageInput.disabled = pending; newButton.disabled = pending; sendButton.disabled = pending;
    };
    const addCurrentMessage = (message, forceBottom = false) => { const followBottom = forceBottom || nearConversationBottom(); const current = activeConversation() || createConversation(); current.messages.push(safeMessage(message)); current.messages = current.messages.filter(Boolean).slice(-MAX_MESSAGES); current.updatedAt = nowIso(); persistStore(); renderConversation(followBottom); };
    const startNew = () => { createConversation(); closeHistory(); renderConversation(true); };

    form.addEventListener("submit", async (event) => {
        event.preventDefault(); if (requestPending || isHistoricalConversation()) return;
        const text = messageInput.value; if (!text.trim()) return;
        addCurrentMessage({role: "user", text, evidence: []}, true); messageInput.value = ""; resizeComposer(); setPending(true); statusText.textContent = "ShiftLight düşünüyor…"; statusText.classList.remove("error");
        try {
            const data = await fetchJson("/api/ai/chat", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({message: text})});
            if (typeof data.answer !== "string" || !data.answer.trim() || !Array.isArray(data.evidence)) throw new ShiftLightUIError("generic");
            addCurrentMessage({role: "assistant", text: data.answer, evidence: safeEvidence(data.evidence)}); statusText.textContent = "";
        } catch (error) {
            const message = safeError(error); statusText.textContent = message; statusText.classList.add("error");
            const shell = assistantShell(); shell.article.classList.add("error"); addText(shell.answer, "p", "", message); conversation.appendChild(shell.article); conversation.scrollTop = conversation.scrollHeight;
        } finally { setPending(false); messageInput.focus(); }
    });
    launcher.addEventListener("click", openDrawer); flightMascot.addEventListener("click", openDrawer); flightMascot.addEventListener("animationend", (event) => { if (["shiftlight-flight", "shiftlight-flight-mobile"].includes(event.animationName) && flightMascot.dataset.state === "flying") landMascot(welcomeRun); }); closeButton.addEventListener("click", closeDrawer); expandButton.addEventListener("click", () => fullscreen ? closeFullscreen() : openFullscreen()); overlay.addEventListener("click", () => fullscreen ? closeFullscreen() : closeDrawer());
    nudgeClose.addEventListener("click", () => collapseWelcome(welcomeRun)); newButton.addEventListener("click", () => { if (!requestPending) startNew(); });
    historyToggle.addEventListener("click", toggleHistory); historyClose.addEventListener("click", closeHistory);
    messageInput.addEventListener("input", resizeComposer); messageInput.addEventListener("keydown", (event) => { if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); if (!requestPending) form.requestSubmit(); } });
    document.addEventListener("keydown", (event) => { if (event.key !== "Escape" || !drawer.classList.contains("open")) return; if (fullscreen) closeFullscreen(); else closeDrawer(); });
    renderConversation(); setPending(false); showNudge(); if (root.dataset.openOnLoad === "true") openDrawer();
})();
