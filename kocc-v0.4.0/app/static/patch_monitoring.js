(() => {
    "use strict";

    const root = document.getElementById("patch-app");
    if (!root) return;

    const PAGE_SIZE = 50;
    const ERROR_MESSAGES = {
        patch_timeout: "Backend yanıt vermiyor.",
        patch_unavailable: "Central Patch Monitor bağlantısı kurulamadı.",
        patch_authorization_failed: "Patch Monitor yetkilendirmesi başarısız.",
        patch_incompatible: "Patch Monitor API'si beklenen sözleşmeyle yanıt vermedi.",
        patch_conflict: "İşlem mevcut oturum durumunda gerçekleştirilemiyor.",
        patch_validation_failed: "Akış ayarları doğrulanamadı. Alanları kontrol edin.",
        patch_request_rejected: "Patch Monitor isteği reddetti.",
    };
    const state = {
        sessionId: localStorage.getItem("koccPatchSession") || "",
        timer: null,
        stream: null,
        expired: false,
        refreshing: false,
        designs: [],
        sessions: [],
        selectedDesign: null,
        cursors: {images:"",targets:"",changes:""},
    };
    const byId = id => document.getElementById(id);
    const esc = value => String(value ?? "").replace(/[&<>"']/g, char => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"})[char]);
    const status = value => `<span class="${window.KOCCTheme.statusClass(value)}">${esc(value || "Unknown")}</span>`;
    const humanTime = value => value ? new Date(Number(value) * 1000).toLocaleString("tr-TR", {timeZone:"Europe/Istanbul"}) : "Henüz gözlem yok";
    const clusterObservation = cluster => cluster.error ? "Son tarama başarısız; önceki güvenli veri korunuyor." : humanTime(cluster.observed);
    const expire = () => {
        if (state.expired) return;
        state.expired = true;
        if (state.timer) clearInterval(state.timer);
        if (state.stream) state.stream.close();
        location.assign(`/login?next=${encodeURIComponent(location.pathname + location.search)}`);
    };
    const api = async (path, options = {}) => {
        let response;
        try {
            response = await fetch(path, {...options,credentials:"same-origin",headers:{"Accept":"application/json","Content-Type":"application/json",...(options.headers || {})}});
        } catch (_) {
            throw new Error(ERROR_MESSAGES.patch_unavailable);
        }
        if (response.status === 401) {
            expire();
            throw new Error("Oturum süresi doldu.");
        }
        const data = await response.json().catch(() => ({}));
        if (!response.ok) throw new Error(ERROR_MESSAGES[data.error] || (response.status >= 500 ? ERROR_MESSAGES.patch_unavailable : "Patch Monitor isteği tamamlanamadı."));
        return data;
    };
    const showAvailable = () => {
        byId("patch-loading").hidden = true;
        byId("patch-error-state").hidden = true;
        byId("patch-content").hidden = false;
        byId("patch-connection").className = "status-success";
        byId("patch-connection").textContent = "Kullanılabilir";
    };
    const showError = message => {
        byId("patch-loading").hidden = true;
        byId("patch-content").hidden = true;
        byId("patch-error-message").textContent = message;
        byId("patch-error-state").hidden = false;
        byId("patch-connection").className = "status-danger";
        byId("patch-connection").textContent = "Kullanılamıyor";
    };
    const table = (headers, rows) => rows.length ? `<table class="patch-table"><thead><tr>${headers.map(esc).map(x => `<th>${x}</th>`).join("")}</tr></thead><tbody>${rows.map(row => `<tr>${row.map(value => `<td>${value}</td>`).join("")}</tr>`).join("")}</tbody></table>` : '<div class="patch-empty">Gösterilecek veri yok.</div>';
    const setSession = id => {
        state.sessionId = id || "";
        if (id) localStorage.setItem("koccPatchSession", id);
        else localStorage.removeItem("koccPatchSession");
        state.cursors = {images:"",targets:"",changes:""};
    };
    const currentSettings = () => ({
        target_tag:byId("patch-target").value.trim(),
        flow:byId("patch-flow").value,
        clusters:[...document.querySelectorAll('[name="patch-cluster"]:checked')].map(input => input.value),
        duration_minutes:Number(byId("patch-duration").value),
        namespace_glob:byId("patch-namespace-glob").value.trim() || "*",
        namespaces:byId("patch-namespaces").value.split(",").map(value => value.trim()).filter(Boolean),
        interval_seconds:byId("patch-interval").value ? Number(byId("patch-interval").value) : null,
        tag_match_mode:byId("patch-tag-mode").value,
    });
    const renderLocalPreview = () => {
        const output = byId("patch-preview-output");
        if (!output) return;
        const value = currentSettings();
        output.innerHTML = `<strong>${esc(value.target_tag || "Hedef sürüm seç")}</strong> için <strong>${value.clusters.length} cluster</strong><br>${esc(value.clusters.join(", ") || "Cluster seçilmedi")}<br>Namespace: <strong>${esc(value.namespace_glob)}</strong>${value.namespaces.length ? ` · ${esc(value.namespaces.join(", "))}` : ""}<br>${value.interval_seconds ? `Her <strong>${value.interval_seconds} saniyede</strong>, ` : ""}<strong>${value.duration_minutes} dakika</strong>`;
    };
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
        renderLocalPreview();
    };
    const renderDesigns = designs => {
        state.designs = designs || [];
        const output = byId("patch-designs");
        output.innerHTML = state.designs.length ? state.designs.map((design, index) => `<button type="button" class="patch-design-card" data-design="${index}"><strong>${esc(design.name)}</strong><small>${esc(design.description || "Kayıtlı patch akışı")}</small><small>Hedef ${esc(design.settings?.target_tag || "—")} · ${esc(design.settings?.namespace_glob || "*")}</small><span>Bu akışı kullan →</span></button>`).join("") : '<div class="patch-empty">Henüz kayıtlı akış yok. Aşağıdan yeni bir akış oluşturabilirsiniz.</div>';
        output.querySelectorAll("[data-design]").forEach(button => {
            button.onclick = () => {
                output.querySelectorAll("[data-design]").forEach(item => item.classList.remove("selected"));
                button.classList.add("selected");
                state.selectedDesign = Number(button.dataset.design);
                applySettings(state.designs[state.selectedDesign].settings);
            };
        });
    };
    const renderServerPreview = preview => {
        byId("patch-preview-output").innerHTML = `<strong>${esc(preview.target_tag)}</strong> için <strong>${(preview.clusters || []).length} cluster</strong><br>${esc((preview.clusters || []).join(", "))}<br>Namespace: <strong>${esc(preview.scope?.namespace_glob || "*")}</strong>${preview.scope?.namespaces?.length ? ` · ${esc(preview.scope.namespaces.join(", "))}` : ""}<br>Her <strong>${esc(preview.interval_seconds)} saniyede</strong>, <strong>${esc(preview.duration_minutes)} dakika</strong>`;
        if (preview.steps?.length) byId("patch-preview-steps").innerHTML = preview.steps.map(step => `<li>${esc(step)}.</li>`).join("");
    };
    const watchSession = () => {
        if (state.timer) clearInterval(state.timer);
        state.timer = setInterval(async () => {
            try {
                const session = await api(`/api/patch/sessions/${encodeURIComponent(state.sessionId)}`);
                renderSessionControl(session);
                if (!["CAPTURING","STOPPING"].includes(session.status)) { clearInterval(state.timer); state.timer = null; }
            } catch (error) { showError(error.message); }
        }, 3000);
    };
    const renderSessionControl = session => {
        const output = byId("patch-session-control");
        if (!output) return;
        const complete = (session.clusters || []).every(cluster => cluster.baseline);
        const action = session.status === "DRAFT" || (session.status === "INTERRUPTED" && !complete) ? "baseline" : session.status === "BASELINE_READY" || (session.status === "INTERRUPTED" && complete) ? "start" : ["RUNNING","CAPTURING"].includes(session.status) ? "stop" : null;
        const labels = {baseline:"Baseline al",start:"Canlı izlemeyi başlat",stop:"İzlemeyi durdur"};
        const hints = {DRAFT:"Başlangıç durumunu kaydetmeye hazır.",CAPTURING:"Başlangıç durumu kaydediliyor.",BASELINE_READY:"Baseline hazır; canlı izleme başlatılabilir.",RUNNING:"Canlı tarama devam ediyor.",STOPPING:"Tarama güvenli şekilde durduruluyor.",STOPPED:"Tarama durduruldu.",COMPLETED:"İzleme süresi tamamlandı.",INTERRUPTED:"Kesilen oturum kaldığı yerden devam ettirilebilir."};
        output.innerHTML = `<div><strong>Hedef ${esc(session.target || session.target_tag || "—")}</strong> · ${status(session.status)}<p>${esc(hints[session.status] || "Oturum durumu güncel.")}</p><small>${esc((session.clusters || []).map(cluster => cluster.cluster || cluster).join(", "))} · ${esc(session.id?.slice(0, 8) || "")}</small></div>${action ? `<button type="button" class="${action === "stop" ? "" : "patch-primary"}" data-session-action="${action}">${labels[action]}</button>` : ""}`;
        output.querySelector("[data-session-action]")?.addEventListener("click", async event => {
            try {
                const updated = await api(`/api/patch/sessions/${encodeURIComponent(session.id)}/${event.currentTarget.dataset.sessionAction}`, {method:"POST"});
                renderSessionControl(updated);
                if (["CAPTURING","STOPPING"].includes(updated.status)) watchSession();
            } catch (error) { showError(error.message); }
        });
    };
    const bootstrapFlow = (clusters, flows) => {
        const items = clusters.items || [];
        byId("patch-clusters").innerHTML = items.length ? items.map((item, index) => `<label class="cluster-chip"><input type="checkbox" name="patch-cluster" value="${esc(item.id)}" ${item.id === root.dataset.koccCluster || (!items.some(cluster => cluster.id === root.dataset.koccCluster) && index === 0) ? "checked" : ""}>${esc(item.id)}</label>`).join("") : '<span class="patch-muted">Kullanılabilir cluster bulunamadı.</span>';
        const templates = flows.templates || {};
        byId("patch-flow").innerHTML = Object.keys(templates).map(name => `<option value="${esc(name)}">${esc(name)}</option>`).join("");
        const firstTemplate = templates[byId("patch-flow").value];
        if (firstTemplate) byId("patch-interval").value = firstTemplate.interval_seconds;
        renderDesigns(flows.designs);
        byId("patch-flow-form").addEventListener("input", renderLocalPreview);
        byId("patch-preview").onclick = async () => {
            try { renderServerPreview(await api("/api/patch/flows/preview", {method:"POST",body:JSON.stringify(currentSettings())})); }
            catch (error) { showError(error.message); }
        };
        byId("patch-save").onclick = async () => {
            const selected = state.selectedDesign == null ? null : state.designs[state.selectedDesign];
            const name = prompt("Akış tasarımı adı", selected?.name || "");
            if (!name) return;
            try {
                await api("/api/patch/flows/designs", {method:"POST",body:JSON.stringify({name,description:selected?.description || "KOCC patch akışı",settings:currentSettings()})});
                const updated = await api("/api/patch/flows");
                state.selectedDesign = null;
                renderDesigns(updated.designs);
            } catch (error) { showError(error.message); }
        };
        byId("patch-create").onclick = async () => {
            try {
                const session = await api("/api/patch/sessions", {method:"POST",body:JSON.stringify(currentSettings())});
                setSession(session.id);
                byId("patch-session-panel").hidden = false;
                renderSessionControl(session);
                byId("patch-session-panel").scrollIntoView({behavior:"smooth",block:"nearest"});
            } catch (error) { showError(error.message); }
        };
        renderLocalPreview();
    };
    const prepareSessions = sessions => {
        state.sessions = sessions.items || [];
        if (!state.sessions.some(session => session.id === state.sessionId)) setSession(state.sessions[0]?.id || "");
        const select = byId("patch-session-select");
        if (select) {
            select.innerHTML = state.sessions.map(session => `<option value="${esc(session.id)}" ${session.id === state.sessionId ? "selected" : ""}>Hedef ${esc(session.target)} · ${esc(session.status)}</option>`).join("");
            select.onchange = () => { setSession(select.value); refreshView(); connectStream(); };
        }
    };
    const showSessionArea = hasSession => {
        byId("patch-no-session").hidden = hasSession;
        const content = root.dataset.view === "live" ? byId("patch-live-content") : byId("patch-compare-content");
        if (content) content.hidden = !hasSession;
    };
    const query = values => new URLSearchParams(Object.entries(values).filter(([, value]) => value !== "" && value != null)).toString();
    const renderPagination = (resource, nextCursor) => {
        const element = byId(`patch-${resource}-pagination`);
        if (!element) return;
        element.innerHTML = `<button type="button" data-page="first" ${state.cursors[resource] ? "" : "disabled"}>İlk sayfa</button><button type="button" data-page="next" ${nextCursor ? "" : "disabled"}>Sonraki</button>`;
        element.querySelector('[data-page="first"]').onclick = () => { state.cursors[resource] = ""; refreshView(); };
        element.querySelector('[data-page="next"]').onclick = () => { state.cursors[resource] = nextCursor || ""; refreshView(); };
    };
    const summaryTotals = rows => (rows || []).reduce((totals, row) => {
        const count = Number(row.count) || 0;
        if (row.row_type === "pod") {
            totals.total += count;
            if (!row.tag_known) totals.unknown += count;
            else if (!row.target_match) totals.old += count;
        }
        if (row.target_match) {
            if (row.health === "READY") totals.ready += count;
            else if (["CRASH","ERROR","IMAGE_PULL_ERROR"].includes(row.health)) totals.bad += count;
            else totals.pending += count;
        }
        return totals;
    }, {total:0,ready:0,bad:0,old:0,unknown:0,pending:0});
    const inspectRow = async rowId => {
        try {
            const detail = await api(`/api/patch/sessions/${encodeURIComponent(state.sessionId)}/containers/${encodeURIComponent(rowId)}`);
            const current = detail.current || {};
            byId("patch-detail").innerHTML = `<strong>${esc(current.workload || "—")} / ${esc(current.container || "—")}</strong><p>${esc(current.cluster || "—")} · ${esc(current.namespace || "—")}</p><p>Şimdi: ${esc(current.image || "—")} · ${status(current.health)}</p><p>Baseline satırı: ${(detail.baseline || []).length} · Restart farkı: ${esc(detail.restart_delta ?? "—")}</p>`;
            byId("patch-detail-panel").hidden = false;
            byId("patch-detail-panel").scrollIntoView({behavior:"smooth",block:"nearest"});
        } catch (error) { showError(error.message); }
    };
    const rowCells = row => [`<strong>${esc(row.workload)}</strong><small>${esc(row.cluster)} · ${esc(row.namespace)}</small>`,esc(row.container),`${esc(row.image_tag || "Tag bilinmiyor")}<small>${esc(row.image || row.image_ref)}</small>`,`${status(row.health)}<small>${esc(row.reason || "")}</small>`,`<button type="button" class="patch-detail-button" data-row-id="${esc(row.id)}">İncele</button>`];
    const loadLive = async () => {
        if (!state.sessionId) return;
        const common = {limit:PAGE_SIZE,search:byId("patch-search").value,health:byId("patch-health").value};
        const [session, summary, images, targets] = await Promise.all([
            api(`/api/patch/sessions/${state.sessionId}`),
            api(`/api/patch/sessions/${state.sessionId}/summary`),
            api(`/api/patch/sessions/${state.sessionId}/images?${query({...common,cursor:state.cursors.images})}`),
            api(`/api/patch/sessions/${state.sessionId}/targets?${query({...common,cursor:state.cursors.targets})}`),
        ]);
        renderSessionControl(session);
        byId("patch-session-context").textContent = `Hedef ${session.target} · ${(session.clusters || []).map(cluster => cluster.cluster).join(", ")} · ${session.scope?.namespace_glob || "*"}`;
        const totals = summaryTotals(summary.counts);
        const cards = [["Görüntülenen container",totals.total],["Hedef sürümde sağlıklı",totals.ready],["Hedef sürümde sorunlu",totals.bad],["Hedef dışında kalan",totals.old],["Tag'i bilinmeyen",totals.unknown],["Hedefte hazır değil",totals.pending]];
        byId("patch-live-summary").innerHTML = cards.map(([label,value]) => `<article class="patch-card"><span>${label}</span><strong>${value}</strong></article>`).join("");
        const strip = byId("patch-cluster-status");
        strip.hidden = !(summary.clusters || []).length;
        strip.innerHTML = (summary.clusters || []).map(cluster => `<article class="patch-cluster-item"><strong>${esc(cluster.cluster)}</strong> ${status(cluster.freshness)}<p>${esc(clusterObservation(cluster))}</p></article>`).join("");
        byId("patch-live-table").innerHTML = table(["Cluster / Uygulama","Container","Image","Sağlık","Detay"], (images.items || []).map(rowCells));
        byId("patch-target-table").innerHTML = table(["Cluster / Uygulama","Container","Image","Sağlık","Detay"], (targets.items || []).map(rowCells));
        renderPagination("images", images.next_cursor);
        renderPagination("targets", targets.next_cursor);
        root.querySelectorAll("[data-row-id]").forEach(button => { button.onclick = () => inspectRow(button.dataset.rowId); });
    };
    const loadCompare = async () => {
        if (!state.sessionId) return;
        const session = await api(`/api/patch/sessions/${state.sessionId}`);
        const data = await api(`/api/patch/sessions/${state.sessionId}/changes?${query({limit:PAGE_SIZE,cursor:state.cursors.changes,version_status:byId("patch-version-status").value,health_change:byId("patch-health-change").value})}`);
        byId("patch-compare-context").textContent = `Hedef ${session.target} · ${(session.clusters || []).map(cluster => cluster.cluster).join(", ")}`;
        byId("patch-compare-table").innerHTML = table(["Cluster / Uygulama","Başlangıçta","Şimdi","Sürüm durumu","Sağlık değişimi"], (data.items || []).map(row => [`<strong>${esc(row.workload)}</strong><small>${esc(row.cluster)} · ${esc(row.namespace)} · ${esc(row.container)}</small>`,`${esc(row.before_images || "—")}<small>${row.before_ready ?? 0} hazır · ${row.before_errors ?? 0} hata</small>`,`${esc(row.after_images || "—")}<small>${row.after_ready ?? 0} hazır · ${row.after_errors ?? 0} hata</small>`,status(row.version_status),status(row.health_change)]));
        renderPagination("changes", data.next_cursor);
    };
    const renderHistory = () => {
        const output = byId("patch-history-list");
        output.innerHTML = state.sessions.length ? state.sessions.map(session => `<article class="patch-history-item"><div><strong>Hedef ${esc(session.target)} · ${esc(session.flow)}</strong><small>${humanTime(session.created)} · ${esc(session.id.slice(0, 8))}</small></div><div>${status(session.status)}<a href="/patch-monitoring/live?cluster=${encodeURIComponent(root.dataset.koccCluster)}" data-session="${esc(session.id)}">Oturumu aç →</a></div></article>`).join("") : '<div class="patch-empty">Henüz oturum yok. Akış Tasarla sekmesinden başlayın.</div>';
        output.querySelectorAll("[data-session]").forEach(link => { link.onclick = () => setSession(link.dataset.session); });
    };
    const connectStream = () => {
        if (!state.sessionId || !window.EventSource) return;
        if (state.stream) state.stream.close();
        state.stream = new EventSource(`/api/patch/sessions/${encodeURIComponent(state.sessionId)}/stream`);
        state.stream.addEventListener("revision", refreshView);
        state.stream.onerror = () => { state.stream.close(); state.stream = null; };
    };
    const refreshView = async () => {
        if (state.expired || state.refreshing || !state.sessionId) return;
        state.refreshing = true;
        try {
            if (root.dataset.view === "live") await loadLive();
            if (root.dataset.view === "compare") await loadCompare();
        } catch (error) { showError(error.message); }
        finally { state.refreshing = false; }
    };
    const bootstrap = async () => {
        byId("patch-loading").hidden = false;
        byId("patch-error-state").hidden = true;
        try {
            await api("/api/patch/config");
            const [clusters, flows, sessions] = await Promise.all([api("/api/patch/clusters"),api("/api/patch/flows"),api("/api/patch/sessions")]);
            prepareSessions(sessions);
            showAvailable();
            if (root.dataset.view === "flow") bootstrapFlow(clusters, flows);
            else if (root.dataset.view === "history") renderHistory();
            else {
                showSessionArea(Boolean(state.sessionId));
                if (state.sessionId) {
                    await refreshView();
                    connectStream();
                    state.timer = setInterval(refreshView, 15000);
                }
            }
        } catch (error) { showError(error.message); }
    };

    byId("patch-retry").onclick = bootstrap;
    byId("patch-detail-close")?.addEventListener("click", () => { byId("patch-detail-panel").hidden = true; });
    [byId("patch-search"),byId("patch-health"),byId("patch-version-status"),byId("patch-health-change")].filter(Boolean).forEach(control => {
        control.onchange = () => { state.cursors = {images:"",targets:"",changes:""}; refreshView(); };
    });
    window.addEventListener("pagehide", () => { if (state.timer) clearInterval(state.timer); if (state.stream) state.stream.close(); });
    bootstrap();
})();
