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

    root.KoccMissingResources = {buildMissingResourcesView};
    if (typeof module !== "undefined" && module.exports) {
        module.exports = root.KoccMissingResources;
    }
}(typeof globalThis !== "undefined" ? globalThis : window));
