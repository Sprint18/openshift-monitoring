(function (root) {
    "use strict";

    const buildMissingResourcesView = (canonicalRecords, options) => {
        const query = String(options.query || "").trim().toLocaleLowerCase("tr");
        const direction = options.sortDirection === "desc" ? "desc" : "asc";
        const sortKey = options.sortKey || "namespace";
        const pageSize = Math.max(1, Number(options.pageSize) || 50);
        const filtered = canonicalRecords
            .filter((item) => options.includeOpenShift ||
                !String(item.namespace).startsWith("openshift-"))
            .filter((item) => !query || [item.namespace, item.pod, item.container]
                .some((value) => String(value)
                    .toLocaleLowerCase("tr").includes(query)))
            .slice()
            .sort((left, right) => {
                const numeric = sortKey === "missing_count";
                const comparison = numeric
                    ? Number(left[sortKey]) - Number(right[sortKey])
                    : String(left[sortKey]).localeCompare(String(right[sortKey]), "tr");
                return direction === "asc" ? comparison : -comparison;
            });
        const pageCount = Math.max(1, Math.ceil(filtered.length / pageSize));
        const page = Math.min(Math.max(1, Number(options.page) || 1), pageCount);
        const start = (page - 1) * pageSize;
        return {
            records: filtered,
            pageRecords: filtered.slice(start, start + pageSize),
            page,
            pageCount,
            total: filtered.length,
            start,
        };
    };

    const createMissingResourcesController = (options) => {
        let page = 1;
        let sort = "namespace";
        let direction = "asc";
        let requestId = 0;
        let debounceTimer = null;
        let controller = null;

        const status = (defined) => defined
            ? '<span class="defined-mark" title="Defined">✓</span>'
            : '<span class="missing-badge">Missing</span>';
        const escapeHtml = (value) => String(value)
            .replaceAll("&", "&amp;").replaceAll("<", "&lt;")
            .replaceAll(">", "&gt;").replaceAll('"', "&quot;")
            .replaceAll("'", "&#039;");
        const parameters = () => {
            const params = new URLSearchParams({
                cluster: options.cluster,
                q: options.search.value,
                include_openshift: String(options.includeOpenShift.checked),
                sort, direction, page: String(page), page_size: "50",
            });
            return params;
        };
        const load = async () => {
            const currentId = ++requestId;
            if (controller) controller.abort();
            controller = new AbortController();
            options.range.textContent = "Kayıtlar filtreleniyor...";
            try {
                const response = await fetch(
                    `${options.endpoint}?${parameters()}`,
                    {signal: controller.signal, headers: {Accept: "application/json"}}
                );
                if (!response.ok) throw new Error(`HTTP ${response.status}`);
                const data = await response.json();
                if (!isCurrentRequest(currentId, requestId)) return;
                page = data.page;
                options.body.innerHTML = data.records.length
                    ? data.records.map((item) => `<tr data-missing-record>
                        <td class="wrap-cell">${escapeHtml(item.namespace)}</td>
                        <td class="wrap-cell">${escapeHtml(item.pod)}</td>
                        <td class="wrap-cell">${escapeHtml(item.container)}</td>
                        <td>${status(item.cpu_request)}</td><td>${status(item.cpu_limit)}</td>
                        <td>${status(item.memory_request)}</td><td>${status(item.memory_limit)}</td>
                        <td class="numeric"><strong>${item.missing_count}</strong></td></tr>`).join("")
                    : '<tr><td colspan="8" class="empty-message">No matching records</td></tr>';
                const start = data.total ? (data.page - 1) * data.page_size + 1 : 0;
                const end = Math.min(data.page * data.page_size, data.total);
                options.range.textContent = `${start}-${end} / ${data.total} kayıt · ${data.total} kayıt bulundu`;
                options.pageNumber.textContent = String(data.page);
                options.previous.disabled = data.page <= 1;
                options.next.disabled = data.page >= data.pages;
            } catch (error) {
                if (error.name !== "AbortError" && currentId === requestId) {
                    console.error("MissingResources request failed:", error);
                    options.range.textContent = "Kayıtlar alınamadı";
                }
            }
        };
        const schedule = () => {
            page = 1;
            window.clearTimeout(debounceTimer);
            debounceTimer = window.setTimeout(load, 350);
        };
        options.search.addEventListener("input", schedule);
        options.includeOpenShift.addEventListener("change", schedule);
        options.previous.addEventListener("click", () => {
            if (page > 1) page -= 1;
            load();
        });
        options.next.addEventListener("click", () => { page += 1; load(); });
        options.sortButtons.forEach((button) => button.addEventListener("click", () => {
            const selected = button.dataset.sort;
            direction = sort === selected && direction === "asc" ? "desc" : "asc";
            sort = selected;
            page = 1;
            load();
        }));
        options.exportButton.addEventListener("click", () => {
            const params = parameters();
            params.delete("page");
            params.delete("page_size");
            window.location.assign(`${options.csvEndpoint}?${params}`);
        });
        load();
        return {load, schedule, getRequestId: () => requestId};
    };

    const isCurrentRequest = (requestId, latestRequestId) =>
        requestId === latestRequestId;

    root.KoccMissingResources = {
        buildMissingResourcesView, createMissingResourcesController,
        isCurrentRequest,
    };
    if (typeof module !== "undefined" && module.exports) {
        module.exports = root.KoccMissingResources;
    }
}(typeof globalThis !== "undefined" ? globalThis : window));
