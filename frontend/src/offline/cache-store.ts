// Save-for-offline data cache: explicit per-query snapshots stored via the
// Cache Storage API, with a small manifest kept in localStorage so the UI can
// list/reopen them without re-reading every cache entry. The service worker
// (public/offline-worker.js) separately caches the app shell (HTML/JS/CSS) so
// the page itself loads offline; this module only manages the data snapshots
// explicitly saved by the user via "Save for offline".
//
// Nothing here fabricates a size or a saved copy: a resource that can't be
// fetched is skipped, and the reported size/count reflect only what was
// actually stored.

const DATA_CACHE_NAME = 'terraflow-offline-data-v1';
const MANIFEST_KEY = 'terraflow.offline.manifest.v1';

export type SavedQuery = {
  siteId: string;
  queryId: string;
  siteName: string;
  savedAt: string; // ISO timestamp
  sizeBytes: number;
  resourceCount: number;
  /** Feature 12: provenance of what was saved (method, run IDs, versions, fallback state). */
  provenance?: Record<string, unknown>;
};

function readManifest(): SavedQuery[] {
  try {
    const raw = localStorage.getItem(MANIFEST_KEY);
    const parsed = raw ? JSON.parse(raw) : [];
    return Array.isArray(parsed) ? parsed : [];
  } catch {
    return [];
  }
}

function writeManifest(entries: SavedQuery[]): void {
  try {
    localStorage.setItem(MANIFEST_KEY, JSON.stringify(entries));
  } catch {
    // Storage unavailable (private mode, quota); the save itself already
    // happened in Cache Storage, only the manifest listing is affected.
  }
}

export function isCacheSupported(): boolean {
  return typeof window !== 'undefined' && 'caches' in window;
}

export function listSavedQueries(): SavedQuery[] {
  return readManifest().sort((a, b) => b.savedAt.localeCompare(a.savedAt));
}

export function getSavedQuery(queryId: string): SavedQuery | null {
  return readManifest().find(entry => entry.queryId === queryId) ?? null;
}

export function latestSavedQuery(): SavedQuery | null {
  return listSavedQueries()[0] ?? null;
}

/** Best-effort Content-Length sum via HEAD requests. A resource whose size
 * can't be determined is counted separately rather than guessed. */
export async function estimateSaveSize(urls: string[]): Promise<{estimatedBytes: number; unknownCount: number}> {
  let estimatedBytes = 0;
  let unknownCount = 0;
  await Promise.all(urls.map(async url => {
    try {
      const response = await fetch(url, {method: 'HEAD'});
      const length = response.headers.get('content-length');
      if (response.ok && length) estimatedBytes += Number(length);
      else unknownCount += 1;
    } catch {
      unknownCount += 1;
    }
  }));
  return {estimatedBytes, unknownCount};
}

export type SaveProgress = {done: number; total: number};

/** Fetches and stores every resource for one query/site under the explicit
 * offline-data cache. A resource that fails to fetch is skipped (best
 * effort); the returned size/count reflect only what was actually stored. */
export async function saveForOffline(
  siteId: string,
  queryId: string,
  siteName: string,
  urls: string[],
  onProgress?: (progress: SaveProgress) => void,
  provenance?: Record<string, unknown>,
): Promise<SavedQuery> {
  if (!isCacheSupported()) throw new Error('This browser does not support offline caching.');
  const cache = await caches.open(DATA_CACHE_NAME);
  const unique = Array.from(new Set(urls));
  let sizeBytes = 0;
  let stored = 0;
  for (let i = 0; i < unique.length; i++) {
    try {
      const response = await fetch(unique[i]);
      if (response.ok) {
        sizeBytes += Number(response.headers.get('content-length')) || 0;
        await cache.put(unique[i], response.clone());
        stored += 1;
      }
    } catch {
      // Resource unreachable right now; leave it out rather than fabricate it.
    }
    onProgress?.({done: i + 1, total: unique.length});
  }
  if (stored === 0) throw new Error('Nothing could be saved for offline use — check the connection and try again.');
  const entry: SavedQuery = {siteId, queryId, siteName, savedAt: new Date().toISOString(), sizeBytes, resourceCount: stored, ...(provenance ? {provenance} : {})};
  writeManifest([entry, ...readManifest().filter(existing => existing.queryId !== queryId)]);
  return entry;
}

/** Drops a query from the manifest. Cache entries are left in the shared
 * bucket (best-effort only); they simply stop being referenced. */
export function deleteSavedQuery(queryId: string): void {
  writeManifest(readManifest().filter(entry => entry.queryId !== queryId));
}

export async function registerOfflineWorker(): Promise<void> {
  if (typeof navigator === 'undefined' || !('serviceWorker' in navigator)) return;
  try {
    await navigator.serviceWorker.register('/offline-worker.js');
  } catch {
    // App shell caching unavailable; live-mode use is unaffected.
  }
}
