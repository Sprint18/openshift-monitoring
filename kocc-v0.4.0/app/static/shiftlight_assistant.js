(() => {
    "use strict";

    const root = document.getElementById("shiftlight-root");
    if (!root) return;

    const SESSION_KEY = "kocc.shiftlight.conversations.v1";
    const NUDGE_KEY = "kocc.shiftlight.nudge-dismissed.v1";
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
    const drawer = document.getElementById("shiftlight-drawer");
    const overlay = document.getElementById("shiftlight-overlay");
    const closeButton = document.getElementById("shiftlight-close");
    const nudge = document.getElementById("shiftlight-nudge");
    const nudgeClose = document.getElementById("shiftlight-nudge-close");
    const clusterSelect = document.getElementById("shiftlight-cluster");
    const previousButton = document.getElementById("shiftlight-previous");
    const newButton = document.getElementById("shiftlight-new");
    const historyNote = document.getElementById("shiftlight-history-note");
    const conversation = document.getElementById("shiftlight-conversation");
    const form = document.getElementById("shiftlight-form");
    const messageInput = document.getElementById("shiftlight-message");
    const sendButton = document.getElementById("shiftlight-send");
    const statusText = document.getElementById("shiftlight-status");
    let requestPending = false;
    let viewMode = "current";
    let supportedClusters = [];

    class ShiftLightUIError {
        constructor(kind) { this.kind = kind; }
    }

    const emptyConversation = (cluster = "") => ({cluster, messages: []});
    const emptyStore = () => ({current: emptyConversation(), previous: null});
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
            return [{tool: item.tool.slice(0, 100), status: "success", facts: safeFacts(item.facts)}];
        }).slice(0, 20);
    };
    const safeMessage = (item) => {
        if (!item || typeof item !== "object" || !["user", "assistant"].includes(item.role) || typeof item.text !== "string") return null;
        const text = item.text.slice(0, MAX_TEXT_LENGTH);
        if (!text.trim()) return null;
        return {role: item.role, text, evidence: item.role === "assistant" ? safeEvidence(item.evidence) : []};
    };
    const safeConversation = (value) => {
        if (!value || typeof value !== "object" || typeof value.cluster !== "string") return emptyConversation();
        const messages = Array.isArray(value.messages) ? value.messages.map(safeMessage).filter(Boolean).slice(-MAX_MESSAGES) : [];
        return {cluster: value.cluster.slice(0, 80), messages};
    };
    const safeStore = (value) => ({
        current: safeConversation(value && value.current),
        previous: value && value.previous ? safeConversation(value.previous) : null
    });
    const loadStore = () => {
        try { return safeStore(JSON.parse(sessionStorage.getItem(SESSION_KEY) || "null")); }
        catch (_error) { return emptyStore(); }
    };
    let store = loadStore();
    const persistStore = () => {
        store = safeStore(store);
        sessionStorage.setItem(SESSION_KEY, JSON.stringify(store));
    };
    const meaningful = (item) => Boolean(item && item.messages.length);
    const archiveCurrent = () => {
        if (meaningful(store.current)) store.previous = safeConversation(store.current);
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
                const wrapper = document.createElement("div"), table = document.createElement("table"), head = document.createElement("thead"), headRow = document.createElement("tr");
                wrapper.className = "shiftlight-table";
                headers.forEach((value) => appendInline(addText(headRow, "th", "", ""), value));
                head.appendChild(headRow); table.appendChild(head);
                const body = document.createElement("tbody");
                rows.forEach((row) => { const tr = document.createElement("tr"); headers.forEach((_value, column) => appendInline(addText(tr, "td", "", ""), row[column] || "")); body.appendChild(tr); });
                table.appendChild(body); wrapper.appendChild(table); target.appendChild(wrapper);
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
    const renderEmpty = () => {
        const empty = document.createElement("div"); empty.className = "shiftlight-empty";
        const content = document.createElement("div"); const mascot = document.createElement("img"); mascot.className = "shiftlight-empty-mascot"; mascot.src = "/static/shiftlight-mascot.png"; mascot.alt = "KKB ShiftLight AI maskotu"; content.appendChild(mascot); addText(content, "h2", "", "KKB ShiftLight AI"); addText(content, "p", "", "OpenShift cluster'ınız hakkında nasıl yardımcı olabilirim?");
        const choices = document.createElement("div"); choices.className = "shiftlight-suggestions";
        suggestions.forEach((text) => { const button = addText(choices, "button", "", text); button.type = "button"; button.disabled = viewMode === "previous"; button.addEventListener("click", () => { messageInput.value = text; messageInput.focus(); resizeComposer(); }); });
        content.appendChild(choices); empty.appendChild(content); conversation.appendChild(empty);
    };
    const displayedConversation = () => viewMode === "previous" ? store.previous : store.current;
    const nearConversationBottom = () => conversation.scrollHeight - conversation.scrollTop - conversation.clientHeight < 80;
    const renderConversation = (forceBottom = false) => {
        const followBottom = forceBottom || nearConversationBottom();
        const previousScrollTop = conversation.scrollTop;
        conversation.replaceChildren();
        const selected = displayedConversation();
        if (!selected || !selected.messages.length) renderEmpty();
        else selected.messages.forEach((item) => {
            if (item.role === "user") addText(conversation, "article", "shiftlight-message user", item.text);
            else { const shell = assistantShell(); renderAnswer(shell.answer, item.text); appendEvidence(shell.article, item.evidence); conversation.appendChild(shell.article); }
        });
        previousButton.hidden = !meaningful(store.previous);
        previousButton.textContent = viewMode === "previous" ? "Güncel Sohbet" : "Önceki Sohbet";
        historyNote.hidden = viewMode !== "previous";
        historyNote.textContent = viewMode === "previous" && store.previous ? `Salt okunur önceki sohbet · ${store.previous.cluster.toUpperCase()}` : "";
        form.hidden = viewMode === "previous";
        requestAnimationFrame(() => {
            conversation.scrollTop = followBottom ? conversation.scrollHeight : previousScrollTop;
        });
    };

    const openDrawer = () => {
        dismissNudge();
        drawer.classList.add("open"); drawer.setAttribute("aria-hidden", "false"); overlay.hidden = false;
        launcher.setAttribute("aria-expanded", "true"); messageInput.focus();
    };
    const closeDrawer = () => {
        drawer.classList.remove("open"); drawer.setAttribute("aria-hidden", "true"); overlay.hidden = true;
        launcher.setAttribute("aria-expanded", "false"); launcher.focus();
    };
    const dismissNudge = () => { nudge.hidden = true; sessionStorage.setItem(NUDGE_KEY, "true"); };
    const showNudge = () => {
        if (sessionStorage.getItem(NUDGE_KEY) === "true" || root.dataset.openOnLoad === "true") return;
        sessionStorage.setItem(NUDGE_KEY, "true");
        nudge.hidden = false;
        window.setTimeout(dismissNudge, 6500);
    };
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
        requestPending = pending; clusterSelect.disabled = pending; messageInput.disabled = pending; newButton.disabled = pending; sendButton.disabled = pending || !clusterSelect.value;
    };
    const addCurrentMessage = (message, forceBottom = false) => { const followBottom = forceBottom || nearConversationBottom(); store.current.messages.push(safeMessage(message)); store.current.messages = store.current.messages.filter(Boolean).slice(-MAX_MESSAGES); persistStore(); renderConversation(followBottom); };
    const startNew = (cluster = clusterSelect.value) => { archiveCurrent(); store.current = emptyConversation(cluster); viewMode = "current"; persistStore(); renderConversation(true); };
    const changeCluster = () => { const selected = clusterSelect.value; if (selected !== store.current.cluster) startNew(selected); statusText.textContent = `Cluster context: ${selected.toUpperCase()}`; };
    const loadClusters = async () => {
        try {
            const data = await fetchJson("/api/ai/clusters");
            supportedClusters = Array.isArray(data.clusters) ? data.clusters.filter((item) => item && item.enabled && typeof item.id === "string" && typeof item.name === "string") : [];
            if (!supportedClusters.length) throw new ShiftLightUIError("unavailable");
            clusterSelect.replaceChildren(); supportedClusters.forEach((item) => clusterSelect.add(new Option(item.name, item.id)));
            const supportedIds = new Set(supportedClusters.map((item) => item.id));
            const portalCluster = root.dataset.initialCluster;
            const selected = supportedIds.has(portalCluster) ? portalCluster : supportedIds.has(store.current.cluster) ? store.current.cluster : supportedClusters[0].id;
            if (!supportedIds.has(store.current.cluster) || (supportedIds.has(portalCluster) && store.current.cluster && store.current.cluster !== portalCluster)) startNew(selected);
            else if (!store.current.cluster) { store.current.cluster = selected; persistStore(); }
            clusterSelect.value = store.current.cluster; clusterSelect.disabled = false; sendButton.disabled = false; renderConversation(true);
        } catch (_error) { clusterSelect.replaceChildren(new Option("ShiftLight kullanılamıyor", "")); statusText.textContent = MESSAGES.unavailable; statusText.classList.add("error"); }
    };

    form.addEventListener("submit", async (event) => {
        event.preventDefault(); if (requestPending || viewMode !== "current") return;
        const text = messageInput.value; if (!text.trim() || !clusterSelect.value) return;
        addCurrentMessage({role: "user", text, evidence: []}, true); messageInput.value = ""; resizeComposer(); setPending(true); statusText.textContent = "ShiftLight düşünüyor…"; statusText.classList.remove("error");
        try {
            const data = await fetchJson("/api/ai/chat", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({cluster: clusterSelect.value, message: text})});
            if (typeof data.answer !== "string" || !data.answer.trim() || !Array.isArray(data.evidence)) throw new ShiftLightUIError("generic");
            addCurrentMessage({role: "assistant", text: data.answer, evidence: safeEvidence(data.evidence)}); statusText.textContent = "";
        } catch (error) {
            const message = safeError(error); statusText.textContent = message; statusText.classList.add("error");
            const shell = assistantShell(); shell.article.classList.add("error"); addText(shell.answer, "p", "", message); conversation.appendChild(shell.article); conversation.scrollTop = conversation.scrollHeight;
        } finally { setPending(false); messageInput.focus(); }
    });
    launcher.addEventListener("click", openDrawer); closeButton.addEventListener("click", closeDrawer); overlay.addEventListener("click", closeDrawer);
    nudgeClose.addEventListener("click", dismissNudge); newButton.addEventListener("click", () => { if (!requestPending) startNew(); });
    previousButton.addEventListener("click", () => { if (!requestPending) { viewMode = viewMode === "previous" ? "current" : "previous"; renderConversation(); } });
    clusterSelect.addEventListener("change", () => { if (!requestPending) changeCluster(); });
    messageInput.addEventListener("input", resizeComposer); messageInput.addEventListener("keydown", (event) => { if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); if (!requestPending) form.requestSubmit(); } });
    document.addEventListener("keydown", (event) => { if (event.key === "Escape" && drawer.classList.contains("open")) closeDrawer(); });
    renderConversation(); loadClusters(); showNudge(); if (root.dataset.openOnLoad === "true") openDrawer();
})();
