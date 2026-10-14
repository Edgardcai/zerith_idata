# Collection dashboard (192.168.10.45)

Entry points: the workbench's 数据看板 tab and `/collection-dashboard`.
The configured local source is `/data/zerith_data`. No connection to the other robot is used.

## Counting rules

- One original HDF5 episode contributes one record. Left/right columns describe its task targets, not independently collected or split episodes.
- Product totals count distinct original episodes involving that product. The same product on both hands counts once in its product total and once in each side column. Different product rows overlap and must not be summed as the collection total.
- Every chart, filter, record and CSV uses the effective grade: saved human review > latest automatic/QC/VLM result > original collection grade. Human choices and collection baselines come from `runtime/var/grade-selections`, associated by dataset scope and episode UUID. Automatic grades use the latest dataset batch summary and its episode report (`qc_original.quality_grade` before top-level `quality_grade`), projected through the same grading policy as the review workspace. Incomplete QC contributes no automatic grade. The original collection fallback reads `review.json.grade`, then collection metadata. Records retain all three grades and the adopted source; unknown grades remain in the percentage denominator.
- `collection_ts` is converted to Asia/Shanghai. Directory dates are a marked fallback; file modification times never define collection dates.
- Source UUID collisions only deduplicate after HDF5 and all MP4 hashes, date, targets and grade agree. Conflicting identities are retained and flagged. No original files are deleted.
- Hidden, partial, backup, LeRobot and recognized hand-export directories are excluded. Symlink directories and symlink HDF5 records are not traversed. HDF5 locked by an active recorder and files changing during metadata reads are deferred.
- Historical single-hand recordings stay in the original-record total and have only their applicable target side populated.

## Index and operation

The background inventory stores only metadata and cached fingerprints in `runtime/var/collection-dashboard/index.json`. Each scan checks metadata file signatures and re-parses changed entries; hashing of duplicate candidates is cached. Review choices and QC report signatures are checked on every scan even when collection metadata has not changed. Duplicate detection uses the original collection metadata before applying later human/QC grades, so grading cannot add collected records. Completed snapshots replace the old cache atomically. On root/traversal failure, the previous successful snapshot remains available with an explicit error.

Opening the dashboard refreshes at most once per minute, and the update button requests an immediate scan. Partial scan progress does not replace the previous snapshot. The date view starts with today in Asia/Shanghai. The 今天 / 昨天 / 近7天 / 全部 shortcuts select a day, the seven calendar dates ending today, or the full inventory. A custom start/end range includes both boundary dates. Metrics, product quantities, record drill-down and CSV all use the selected range and grade. Only the explicit 全部 shortcut removes date restrictions; incomplete or reversed custom dates do not fetch all-time data. The summary API retains historical trend fields for compatibility, but they are not mixed into the date view.

## Collection plans and display

The dashboard has two independent primary views: 按日期 and 按计划. The date view contains four quick ranges, a start/end picker, four metrics for the selected range, and a per-product quantity table. The plan view contains the three batch targets and all-time product quota comparisons. Each view fetches only its own summary and retains its own selection state. Below the date metrics, a daily stacked column chart shows the original episode counts and a donut shows adopted-grade proportions. Both follow the selected dates and grade filter. Missing days within ranges up to 730 days are zero-filled; broader ranges show collection dates. A/B/F use separate colors, while C and unrated records remain visible and in the denominator. Hover or keyboard focus shows daily counts and percentages. The daily CSV uses the same selected scope.

Record drill-down opens in a modal without switching the parent view. From the date view it retains the selected date range, product and grade. From the plan view it queries the selected product across all dates, retaining the all/A-B plan basis. Pagination and CSV use exactly the same scope. Product grade detail, record grade provenance, manual notes and original video remain available in dialogs.

Plans are read directly from `/data/plan/第一批.docx`, `第二批.docx`, and `第三批.docx` (override with `DATAQC_PLAN_ROOT`). File signatures invalidate the document cache, so replacing a plan is reflected on the next refresh. Missing, malformed or duplicate-product plan documents appear as pending rather than fabricated progress. Source Word documents and per-batch comparison CSVs can be downloaded.

The latest plans contain 2,880 / 3,600 / 3,840 paired collection episodes, covering 24 / 23 / 24 products. Goals are aggregated from task tables rather than inferred from a uniform product quota: the second batch has unequal left/right bottom-shelf targets and the third has 160 per hand for each product. Assessment-only scene 8 in batches two and three has no collection tasks and contributes no target.

Actuals match normalized product names across all dates in the original-record inventory. Every product retains its own left/right planned count, actual count and nonnegative shortage. Overall quota completion credits `min(actual, target)` for each product and each hand; surplus on one side cannot erase a shortage elsewhere. A same-item two-hand episode contributes once to involved episodes and once to each side quota. Records involving two batches may contribute to both batch involvement totals, so batch actuals are not used as the global collection total.

The plan view can compare all collected records or only records whose adopted grade is A/B, retaining the same plan denominator. The date picker and date-view grade selection do not change the all-time plan scope. `/api/collection-dashboard/plans`, `/plan-document/{batch_id}` and `export.csv?kind=plans` supply this view.

The summary, record pagination, CSV exports and video endpoints are under `/api/collection-dashboard/`. Videos are limited to known indexed record IDs and the three collector camera names. Exported CSV uses a UTF-8 BOM and neutralizes spreadsheet formulas in source text. No model calls or QC/conversion jobs are started by the dashboard.

## Validation

Focused tests: `tests/test_collection_dashboard.py` covers timezone dates, side counts, same-item dual-hand counting, derived-directory exclusion, content-verified duplicates, identity conflicts, cache reload/update/deletion, source outages, malformed metadata, in-progress recording, filtering, pagination, export, media confinement and workbench integration.
Browser verification covers desktop/mobile layouts, the workspace tab, dates and empty ranges, grade filters, product details, CSV download and source video playback. Production deployment uses file-hash guards and atomic replacement with a backup of modified files. Only the QC web service requires restart; the collection service is not restarted.

Chart tooltips follow the pointer with a 16-pixel gap, flip left/up near the viewport edges and remain within the viewport. Moving out of a bar hides the tooltip; scrolling the chart hides stale pointer details and updates a keyboard-focused bar. Native SVG titles are omitted to avoid overlapping browser tooltips; accessible labels and keyboard focus remain available. Both deployed servers passed browser checks for movement, edge positioning, narrow screens and keyboard details. This frontend update requires a page reload and does not restart the service.

## H200 hourly replica

Set `DATAQC_DASHBOARD_SOURCE_URL=http://192.168.10.45:9990` on the H200 web service. The existing workbench then adds the same 数据看板 tab while its QC data roots stay configured for H200. A background thread starts with the web service and fetches `/api/collection-dashboard/snapshot` once at startup when due, then once every 3,600 seconds from the previous attempt. It runs even when no browser is open. The next attempt time is persisted before the HTTP request, so a service restart cannot retry an already attempted hour. HTTP retries and redirects are disabled. Browser refresh, filters, pagination, CSV and plan downloads read the local replica and never cause another upstream update.

Each response contains one complete collection inventory, both all/A-B plan comparisons and the source Word documents. It excludes HDF5/video contents and scan/hash internals. Snapshot validation and an atomic, fsynced replacement protect `runtime/var/collection-dashboard/remote.json`. A timeout, HTTP error, malformed response or cache write failure retains the previous successful snapshot and displays an error; the next attempt remains one hour after the previous attempt. Before the first success, the dashboard shows zero records and pending plans with the error rather than fabricating data. The header shows source data time, last successful synchronization and next scheduled attempt. Explicit video playback redirects to the original source and requires that source to be reachable; it does not affect the hourly metadata schedule.

Focused replica tests: `tests/test_dashboard_sync.py` covers failed-hour suppression, persistence across restarts, recovery, incomplete-response rejection, source outage, independent background updates, cached filtering/exports/plans/documents and no upstream requests from UI routes.
