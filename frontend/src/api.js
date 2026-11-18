async function req(path, opts = {}) {
  const res = await fetch(path, {
    headers: { 'Content-Type': 'application/json', ...(opts.headers || {}) },
    ...opts,
  })
  if (!res.ok) {
    const text = await res.text()
    throw new Error(text || res.statusText)
  }
  return res.json()
}

export const api = {
  health: () => req('/api/health'),
  overview: () => req('/api/overview'),
  waybills: () => req('/api/waybills'),
  timeline: (id) => req(`/api/waybills/${encodeURIComponent(id)}/timeline`),
  quarantine: () => req('/api/quarantine'),
  sidecar: () => req('/api/sidecar'),
  ingest: () => req('/api/ingest/run', { method: 'POST' }),
  proofs: (waybillId) =>
    req(waybillId ? `/api/status-proofs?waybill_id=${encodeURIComponent(waybillId)}` : '/api/status-proofs'),
  remount: (oldKey, newKey, newTitle) =>
    req('/api/remount', {
      method: 'POST',
      body: JSON.stringify({ old_key: oldKey, new_key: newKey, new_title: newTitle || null }),
    }),
  remounts: () => req('/api/remounts'),
  remountProof: (edgeId) => req(`/api/remounts/${encodeURIComponent(edgeId)}/proof`),
}
