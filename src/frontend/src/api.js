const KEY = 'sm_session'

export const session = {
  get() {
    try { return JSON.parse(localStorage.getItem(KEY) || 'null') } catch { return null }
  },
  set(s) { localStorage.setItem(KEY, JSON.stringify(s)) },
  clear() { localStorage.removeItem(KEY) },
}

const RANK = { viewer: 1, operator: 2, admin: 3 }
export const can = (role) => RANK[session.get()?.role] >= RANK[role]

function detail(data) {
  if (!data) return 'request failed'
  if (typeof data === 'string') return data
  if (data.detail) return typeof data.detail === 'string' ? data.detail : JSON.stringify(data.detail)
  if (data.errors) return data.errors.join('; ')
  return JSON.stringify(data)
}

export async function api(path, { method = 'GET', body, form, raw } = {}) {
  const headers = {}
  const s = session.get()
  if (s?.token) headers.Authorization = `Bearer ${s.token}`
  let payload
  if (form) payload = form
  else if (body !== undefined) {
    headers['Content-Type'] = 'application/json'
    payload = JSON.stringify(body)
  }
  const r = await fetch(path, { method, headers, body: payload })
  if (r.status === 401 && !path.startsWith('/api/auth/login')) {
    session.clear()
    window.location.href = '/login'
    throw new Error('login required')
  }
  if (raw) {
    if (!r.ok) {
      const t = await r.text()
      let d = t
      try { d = JSON.parse(t) } catch { /* plain text */ }
      throw new Error(detail(d))
    }
    return r
  }
  const ct = r.headers.get('content-type') || ''
  const data = ct.includes('json') ? await r.json() : await r.text()
  if (!r.ok) {
    const err = new Error(detail(data))
    err.data = data
    throw err
  }
  return data
}

export async function download(path, fallback = 'download') {
  const r = await api(path, { raw: true })
  const cd = r.headers.get('content-disposition') || ''
  const m = cd.match(/filename="?([^";]+)"?/)
  const blob = await r.blob()
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = m ? m[1] : fallback
  document.body.appendChild(a)
  a.click()
  a.remove()
  setTimeout(() => URL.revokeObjectURL(url), 5000)
}

export const qs = (o) =>
  Object.entries(o).filter(([, v]) => v !== '' && v !== undefined && v !== null)
    .map(([k, v]) => `${encodeURIComponent(k)}=${encodeURIComponent(v)}`).join('&')
