(() => {
    "use strict";

    const root = document.getElementById("patch-app");
    if (!root) return;

    const PAGE_SIZE = 50;
    const state = {
        sessionId: localStorage.getItem("koccPatchSession") || "",
        timer: null,
        stream: null,
        expired: false,
        refreshing: false,
        designs: [],
        cursors: {images: "", targets: "", changes: ""},
    };
    const byId = id => document.getElementById(id);
    const esc = value => String(value ?? "").replace(
        /[&<>"']/g,
        char => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"})[char],
    );
    const status = value => `<span class="${window.KOCCTheme.statusClass(value)}">${esc(value || "Unknown")}</span>`;
    const expire = () => {
        if (state.expired) return;
        state.expired = true;
        if (state.timer) clearInterval(state.timer);
        if (state.stream) state.stream.close();
        const next = encodeURIComponent(location.pathname + location.search);
        location.assign(`/login?next=${next}`);
    };
    const api = async (path, options = {}) => {
        const response = await fetch(path, {
            ...options,
            credentials: "same-origin",
            headers: {"Accept":"application/json","Content-Type":"application/json",...(options.headers || {})},
        });
        if (response.status === 401) {
            expire();
            throw new Error("authentication_required");
        }
        const data = await response.json().catch(() => ({}));
        if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
        return data;
    };
    const alert = message => {
        const element = byId("patch-alert");
        element.textContent = message;
        element.hidden = false;
        byId("patch-connection").className = "status-danger";
        byId("patch-connection").textContent = "Unavailable";
    };
    const available = () => {
        byId("patch-alert").hidden = true;
        byId("patch-connection").className = "status-success";
        byId("patch-connection").textContent = "Available";
    };
    const table = (headers, rows) => rows.length
        ? `<table><thead><tr>${headers.map(esc).map(x => `<th>${x}</th>`).join("")}</tr></thead><tbody>${rows.map(row => `<tr>${row.map(value => `<td>${value}</td>`).join("")}</tr>`).join("")}</tbody></table>`
        : '<div class="empty">No data</div>';
    const setSession = id => {
        state.sessionId = id || "";
        if (id) localStorage.setItem("koccPatchSession", id);
        state.cursors = {images: "", targets: "", changes: ""};
    };
    const settings = () => ({
        target_tag: byId("patch-target").value.trim(),
        flow: byId("patch-flow").value,
        clusters: [...document.querySelectorAll('[name="patch-cluster"]:checked')].map(x => x.value),
        duration_minutes: Number(byId("patch-duration").value),
        namespace_glob: byId("patch-namespace-glob").value.trim() || "*",
        namespaces: byId("patch-namespaces").value.split(",").map(x => x.trim()).filter(Boolean),
        interval_seconds: byId("patch-interval").value ? Number(byId("patch-interval").value) : null,
        tag_match_mode: byId("patch-tag-mode").value,
    });
    const applySettings = value => {
        if (!value) return;
        byId("patch-target").value = value.target_tag || "";
        byId("patch-flow").value = value.flow || byId("patch-flow").value;
        byId("patch-duration").value = value.duration_minutes || 60;
        byId("patch-namespace-glob").value = Array.isArray(value.namespace_glob) ? value.namespace_glob.join(",") : (value.namespace_glob || "*");
        byId("patch-namespaces").value = (value.namespaces || []).join(",");
        byId("patch-interval").value = value.interval_seconds || "";
        byId("patch-tag-mode").value = value.tag_match_mode || "exact";
        const selected = new Set(value.clusters || []);
        document.querySelectorAll('[name="patch-cluster"]').forEach(input => { input.checked = selected.has(input.value); });
    };
    const populateDesigns = designs => {
        state.designs = designs || [];
        const select = byId("patch-design");
        select.innerHTML = '<option value="">Yeni tasarım</option>' + state.designs.map((item, index) => `<option value="${index}">${esc(item.name)}</option>`).join("");
        select.onchange = () => applySettings(state.designs[Number(select.value)]?.settings);
    };
    const renderSessionControl = item => {
        const output = byId("patch-session-control");
        if (!output) return;
        const current = item || {};
        const interruptedActions = (current.clusters || []).every(cluster => cluster.baseline) ? ["start"] : ["baseline"];
        const allowed = {DRAFT:["baseline"],INTERRUPTED:interruptedActions,BASELINE_READY:["start"],RUNNING:["stop"],CAPTURING:["stop"],STOPPING:[]};
        output.innerHTML = `<p><strong>${esc(current.id || "—")}</strong> ${status(current.status || "DRAFT")}</p><p>Target: ${esc(current.target || current.target_tag || "—")} · Cluster: ${esc((current.clusters || []).map(x => x.cluster || x).join(", "))}</p><div>${(allowed[current.status] || []).map(action => `<button class="patch-action" data-session-action="${action}">${({baseline:"Baseline Al",start:"İzlemeyi Başlat",stop:"Durdur"})[action]}</button>`).join(" ")}</div>`;
        output.querySelectorAll("[data-session-action]").forEach(button => {
            button.onclick = async () => {
                try {
                    const data = await api(`/api/patch/sessions/${encodeURIComponent(current.id)}/${button.dataset.sessionAction}`, {method:"POST"});
                    renderSessionControl(data);
                } catch (error) {
                    alert(`İşlem başarısız: ${error.message}`);
                }
            };
        });
    };
    const bootstrapFlow = async () => {
        const [clusters, flows] = await Promise.all([api("/api/patch/clusters"), api("/api/patch/flows")]);
        available();
        const items = clusters.items || [];
        byId("patch-clusters").innerHTML = items.map((item, index) => `<label><input type="checkbox" name="patch-cluster" value="${esc(item.id)}" ${item.id === root.dataset.koccCluster || (!items.some(x => x.id === root.dataset.koccCluster) && index === 0) ? "checked" : ""}>${esc(item.id)}</label>`).join("");
        const templates = flows.templates || {};
        byId("patch-flow").innerHTML = Object.keys(templates).map(name => `<option value="${esc(name)}">${esc(name)}</option>`).join("");
        populateDesigns(flows.designs);
        byId("patch-preview").onclick = async () => {
            try {
                byId("patch-preview-output").textContent = JSON.stringify(await api("/api/patch/flows/preview", {method:"POST",body:JSON.stringify(settings())}), null, 2);
            } catch (error) { alert(`Önizleme başarısız: ${error.message}`); }
        };
        byId("patch-save").onclick = async () => {
            const selectedIndex = byId("patch-design").value;
            const selected = selectedIndex === "" ? null : state.designs[Number(selectedIndex)];
            const name = prompt("Akış tasarımı adı", selected?.name || "");
            if (!name) return;
            try {
                await api("/api/patch/flows/designs", {method:"POST",body:JSON.stringify({name,description:selected?.description || "KOCC",settings:settings()})});
                const updated = await api("/api/patch/flows");
                populateDesigns(updated.designs);
                byId("patch-preview-output").textContent = "Akış tasarımı kaydedildi.";
            } catch (error) { alert(`Kayıt başarısız: ${error.message}`); }
        };
        byId("patch-create").onclick = async () => {
            try {
                const data = await api("/api/patch/sessions", {method:"POST",body:JSON.stringify(settings())});
                setSession(data.id);
                renderSessionControl(data);
            } catch (error) { alert(`Oturum oluşturulamadı: ${error.message}`); }
        };
    };
    const loadSessions = async () => {
        const items = (await api("/api/patch/sessions")).items || [];
        if (!items.some(item => item.id === state.sessionId)) setSession(items[0]?.id || "");
        return items;
    };
    const sessionPicker = async () => {
        const items = await loadSessions();
        const select = byId("patch-session-select");
        if (select) {
            select.innerHTML = items.map(item => `<option value="${esc(item.id)}" ${item.id === state.sessionId ? "selected" : ""}>${esc(item.target || item.target_tag || item.id)} · ${esc(item.status)}</option>`).join("");
            select.onchange = () => { setSession(select.value); refreshView(); connectStream(); };
        }
        return items;
    };
    const query = values => new URLSearchParams(Object.entries(values).filter(([, value]) => value !== "" && value != null)).toString();
    const renderPagination = (resource, nextCursor) => {
        const element = byId(`patch-${resource}-pagination`);
        if (!element) return;
        element.innerHTML = `<button type="button" data-page="first" ${state.cursors[resource] ? "" : "disabled"}>İlk Sayfa</button><button type="button" data-page="next" ${nextCursor ? "" : "disabled"}>Sonraki</button>`;
        element.querySelector('[data-page="first"]').onclick = () => { state.cursors[resource] = ""; refreshView(); };
        element.querySelector('[data-page="next"]').onclick = () => { state.cursors[resource] = nextCursor || ""; refreshView(); };
    };
    const countSummary = rows => (rows || []).reduce((result, row) => {
        const count = Number(row.count) || 0;
        result.total += count;
        if (row.target_match) result.target += count;
        if (["CRASH","ERROR","IMAGE_PULL_ERROR"].includes(row.health)) result.unhealthy += count;
        return result;
    }, {total:0,target:0,unhealthy:0});
    const inspectRow = async rowId => {
        try {
            const detail = await api(`/api/patch/sessions/${encodeURIComponent(state.sessionId)}/containers/${encodeURIComponent(rowId)}`);
            byId("patch-detail").textContent = JSON.stringify(detail, null, 2);
        } catch (error) { alert(`Detay alınamadı: ${error.message}`); }
    };
    const bindDetailButtons = () => document.querySelectorAll("[data-row-id]").forEach(button => { button.onclick = () => inspectRow(button.dataset.rowId); });
    const rowCells = row => [
        `${esc(row.cluster)}<small>${esc(row.namespace)}</small>`, esc(row.workload), esc(row.container),
        esc(row.image || row.image_ref), status(row.health),
        `<button type="button" class="detail-button" data-row-id="${esc(row.id)}">İncele</button>`,
    ];
    const loadLive = async () => {
        if (!state.sessionId) return;
        const common = {limit:PAGE_SIZE,search:byId("patch-search")?.value || "",health:byId("patch-health")?.value || ""};
        const imageQuery = {...common,cursor:state.cursors.images};
        const targetQuery = {...common,cursor:state.cursors.targets};
        const [session, summary, images, targets] = await Promise.all([
            api(`/api/patch/sessions/${state.sessionId}`),
            api(`/api/patch/sessions/${state.sessionId}/summary`),
            api(`/api/patch/sessions/${state.sessionId}/images?${query(imageQuery)}`),
            api(`/api/patch/sessions/${state.sessionId}/targets?${query(targetQuery)}`),
        ]);
        available();
        const counts = countSummary(summary.counts);
        byId("patch-live-summary").innerHTML = [["Status",status(session.status)],["Rows",counts.total],["Target",counts.target],["Unhealthy",counts.unhealthy],["Revision",summary.revision ?? session.revision ?? 0]].map(([label,value]) => `<article class="patch-card"><span>${label}</span><strong>${value}</strong></article>`).join("");
        byId("patch-cluster-status").innerHTML = table(["Cluster","Freshness","Observed","Error"], (summary.clusters || []).map(cluster => [esc(cluster.cluster),status(cluster.freshness),esc(cluster.observed || "—"),`<span class="cluster-error">${esc(cluster.error || "—")}</span>`]));
        byId("patch-live-table").innerHTML = table(["Cluster / Namespace","Workload","Container","Image","Health","Detail"], (images.items || []).map(rowCells));
        byId("patch-target-table").innerHTML = table(["Cluster / Namespace","Workload","Container","Image","Health","Detail"], (targets.items || []).map(rowCells));
        renderPagination("images", images.next_cursor);
        renderPagination("targets", targets.next_cursor);
        bindDetailButtons();
    };
    const loadCompare = async () => {
        if (!state.sessionId) return;
        const values = {limit:PAGE_SIZE,cursor:state.cursors.changes,version_status:byId("patch-version-status").value,health_change:byId("patch-health-change").value};
        const data = await api(`/api/patch/sessions/${state.sessionId}/changes?${query(values)}`);
        available();
        byId("patch-compare-table").innerHTML = table(["Workload","Before","After","Version Status","Health Change"], (data.items || []).map(row => [`${esc(row.workload)}<small>${esc(row.cluster)} · ${esc(row.namespace)} · ${esc(row.container)}</small>`,`${esc(row.before_images || "—")}<small>${row.before_ready ?? 0} ready / ${row.before_errors ?? 0} error</small>`,`${esc(row.after_images || "—")}<small>${row.after_ready ?? 0} ready / ${row.after_errors ?? 0} error</small>`,status(row.version_status),status(row.health_change)]));
        renderPagination("changes", data.next_cursor);
    };
    const loadHistory = async () => {
        const items = await loadSessions();
        available();
        byId("patch-history-table").innerHTML = table(["Target / Flow","Status","Created","Ends","Actions"], items.map(item => [`${esc(item.target)}<small>${esc(item.flow)}</small>`,status(item.status),esc(item.created || "—"),esc(item.ends || "—"),`<a href="/patch-monitoring/compare?cluster=${encodeURIComponent(root.dataset.koccCluster)}" data-session="${esc(item.id)}">İncele</a>`]));
        byId("patch-history-table").querySelectorAll("[data-session]").forEach(link => { link.onclick = () => setSession(link.dataset.session); });
    };
    const connectStream = () => {
        if (!state.sessionId || !window.EventSource) return;
        if (state.stream) state.stream.close();
        state.stream = new EventSource(`/api/patch/sessions/${encodeURIComponent(state.sessionId)}/stream`);
        state.stream.addEventListener("revision", () => refreshView());
        state.stream.onerror = () => { state.stream.close(); state.stream = null; };
    };
    const refreshView = async () => {
        if (state.expired || state.refreshing) return;
        state.refreshing = true;
        try {
            if (root.dataset.view === "live") await loadLive();
            else if (root.dataset.view === "compare") await loadCompare();
        } catch (error) {
            alert(`Patch Monitoring kullanılamıyor: ${error.message}`);
        } finally {
            state.refreshing = false;
        }
    };
    const start = async () => {
        try {
            if (root.dataset.view === "flow") await bootstrapFlow();
            else if (root.dataset.view === "history") await loadHistory();
            else {
                await sessionPicker();
                await refreshView();
                connectStream();
                state.timer = setInterval(refreshView, 15000);
                [byId("patch-search"),byId("patch-health"),byId("patch-version-status"),byId("patch-health-change")].filter(Boolean).forEach(control => {
                    control.onchange = () => { state.cursors = {images:"",targets:"",changes:""}; refreshView(); };
                });
            }
        } catch (error) { alert(`Patch Monitoring kullanılamıyor: ${error.message}`); }
    };

    window.addEventListener("pagehide", () => {
        if (state.timer) clearInterval(state.timer);
        if (state.stream) state.stream.close();
    });
    start();
})();
