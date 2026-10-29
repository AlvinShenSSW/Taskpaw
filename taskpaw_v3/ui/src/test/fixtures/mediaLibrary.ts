// 片库与规则 / 设置 fixture data (#361) in the shapes of `library_routes.py`, `library_store.py`,
// `library_history.py` and `library_import.py`. Names and paths are made up.
import type {
  HistoryReceipt, HistoryRow, ImportPreview, ImportResult, Library, LibraryList, LibraryPerformer, LibraryRules,
  PerformerDetail, PerformerPage, ScanStatus,
} from "../../api";

const library = (over: Partial<Library> & Pick<Library, "id" | "kind" | "path" | "label">): Library => ({
  role: "archive", prefixes: [], position: 1, last_scan_at: "2026-10-30T09:12:00Z", last_scan_ok: true, last_scan_ms: 38000,
  film_count: 3120, free_bytes: 2_300_000_000_000, created_at: "2026-10-30T08:00:00Z", performer_count: 0, missing_count: 0,
  ...over,
});

export const LIB_A = library({ id: "lib-a", kind: "actor", path: "\\\\nas-1\\films\\", label: "主片库", performer_count: 238, missing_count: 3 });
export const LIB_B = library({
  id: "lib-b", kind: "actor", path: "\\\\nas-2\\backup\\", label: "备份库", role: "readonly", position: 2,
  last_scan_ok: false, performer_count: 96, film_count: 880, free_bytes: null,
});
export const CAT_K = library({
  id: "cat-k", kind: "category", path: "\\\\nas-1\\personal\\写真\\OAE\\", label: "写真 · OAE", prefixes: ["OAE"],
  film_count: 86, free_bytes: null,
});
export const LIBRARIES: LibraryList = { items: [LIB_A, LIB_B, CAT_K], db_path: "D:\\TaskPaw\\work\\library.db" };
export const NO_LIBRARIES: LibraryList = { items: [], db_path: "D:\\TaskPaw\\work\\library.db" };

export const RULES: LibraryRules = {
  multi_location: "priority", alias_match: true, preselect_single: true, fuzzy: true, allow_new_actor: true,
  new_actor_library: "lib-a", new_actor_name: "actor", category_prefix: true, second_target_hint: true,
  category_always: true, scan_on_start: true, scan_every_hours: 6,
};

export const SCAN_IDLE: ScanStatus = { running: null, queued: [], results: {} };

export const performer = (over: Partial<LibraryPerformer> & Pick<LibraryPerformer, "id" | "name" | "library_id">): LibraryPerformer => ({
  source_label: over.library_id === "lib-b" ? "备份库" : "主片库",
  unc_path: `${over.library_id === "lib-b" ? LIB_B.path : LIB_A.path}${over.name}`,
  has_thumb: true, aliases: [], alias_ids: [], role: over.library_id === "lib-b" ? "readonly" : "archive",
  is_missing: false, film_count: 10, fuzzy: false, ...over,
});

export const HARU_A = performer({ id: "p-haru-a", name: "Haru", library_id: "lib-a", aliases: ["ハル"], alias_ids: [7], film_count: 42 });
export const HARU_B = performer({ id: "p-haru-b", name: "Haru", library_id: "lib-b", aliases: ["ハル"], alias_ids: [7], film_count: 12 });
export const MIO = performer({ id: "p-mio", name: "Mio", library_id: "lib-a", has_thumb: false, film_count: 18 });
export const NAGI = performer({ id: "p-nagi", name: "Nagi", library_id: "lib-a", is_missing: true, film_count: 5 });
export const PERFORMERS: PerformerPage = { items: [HARU_A, HARU_B, MIO, NAGI], total: 4, page: 1, page_size: 50 };

const row = (over: Partial<HistoryRow> & Pick<HistoryRow, "receipt_id" | "number" | "target_path">): HistoryRow => ({
  film_id: `film-${over.receipt_id}`, operation_id: `op-${over.receipt_id}`, accepted_at: "2026-10-30T11:32:00Z",
  kind: "performer", film_dir: over.number, second_target_path: null,
  cleanup: { input: "verified", workcopy: "verified" }, pending: false, title: null, actors: [], ...over,
});

export const ROW_SECOND = row({
  receipt_id: "r-1", number: "OAE-198", target_path: "\\\\nas-1\\films\\Haru\\OAE-198",
  second_target_path: "\\\\nas-1\\personal\\写真\\OAE\\OAE-198", actors: ["Haru"],
});
export const ROW_CATEGORY = row({ receipt_id: "r-2", number: "OAE-203", kind: "category", target_path: "\\\\nas-1\\personal\\写真\\OAE\\OAE-203" });
export const ROW_MANUAL = row({
  receipt_id: "r-3", number: "DEMO-110", kind: "manual", target_path: "\\\\nas-1\\films\\_合集\\DEMO-110",
  cleanup: { input: "failed", workcopy: "verified" }, pending: true,
});
export const HISTORY = { items: [ROW_SECOND, ROW_CATEGORY, ROW_MANUAL], total: 3, page: 1, page_size: 50 };

export const RECEIPT: HistoryReceipt = {
  ...ROW_SECOND,
  files: [
    { path: "OAE-198.mp4", size: 2_000_000_000, sha256: "9f3a00000000000000000000000000000000000000000000000000000000c21e" },
    { path: "OAE-198.nfo", size: 6000, sha256: "e4c200000000000000000000000000000000000000000000000000000000a019" },
  ],
  message: null,
};

export const HARU_DETAIL: PerformerDetail = { ...HARU_A, locations: [HARU_A, HARU_B], receipts: [ROW_SECOND] };

export const IMPORT_PREVIEW: ImportPreview = {
  path: "C:\\Users\\me\\AppData\\Roaming\\Filmpaw\\library.db",
  roots: [
    { path: "\\\\nas-1\\films", label: "主片库", performers: 238, status: "new" },
    { path: "\\\\nas-2\\library", label: "第二片库", performers: 142, status: "new" },
    { path: "\\\\nas-3\\backup", label: "旧备份", performers: 96, status: "overlap" },
  ],
  aliases: 58, new_aliases: 58,
};
export const IMPORT_RESULT: ImportResult = {
  path: IMPORT_PREVIEW.path, added: ["lib-a", "lib-b"],
  skipped: [{ path: "\\\\nas-3\\backup", reason: "该目录与已有片库互相包含" }], aliases_added: 58,
};
