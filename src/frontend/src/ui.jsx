import { useCallback, useEffect, useRef, useState } from 'react'
import { AlertCircle, AlertTriangle, ArrowLeft, Check, CheckCircle2, ChevronDown, Copy, Info, MoreHorizontal, X } from 'lucide-react'

export function usePoll(fn, ms, deps = []) {
  const [data, setData] = useState(null)
  const [error, setError] = useState('')
  const saved = useRef(fn)
  saved.current = fn
  const gen = useRef(0)
  const load = useCallback(async () => {
    const g = gen.current
    try {
      const d = await saved.current()
      if (g !== gen.current) return
      setData(d)
      setError('')
    } catch (e) {
      if (g === gen.current) setError(e.message)
    }
  }, [])
  const first = useRef(true)
  useEffect(() => {
    gen.current += 1
    if (!first.current) { setData(null); setError('') }
    first.current = false
    load()
    const t = ms ? setInterval(load, ms) : null
    return () => { gen.current += 1; if (t) clearInterval(t) }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [ms, ...deps])
  return [data, load, error, setData]
}

export function useAction(onError) {
  const [busy, setBusy] = useState(false)
  const run = async (fn) => {
    setBusy(true)
    try {
      return await fn()
    } catch (e) {
      onError ? onError(e.message) : toast(e.message, 'bad')
      return undefined
    } finally {
      setBusy(false)
    }
  }
  return [busy, run]
}

export function navigate(path) {
  window.history.pushState({}, '', path)
  window.dispatchEvent(new PopStateEvent('popstate'))
}

export const fmtTime = (ts) => (ts ? new Date(ts * 1000).toLocaleString() : '—')
export const fmtIso = (s) => (s ? new Date(s).toLocaleString() : '—')
export function ago(ts) {
  if (!ts) return 'never'
  const s = Math.max(0, Date.now() / 1000 - ts)
  if (s < 60) return `${Math.round(s)}s ago`
  if (s < 3600) return `${Math.round(s / 60)}m ago`
  if (s < 86400) return `${Math.round(s / 3600)}h ago`
  return `${Math.round(s / 86400)}d ago`
}
export const fmtBytes = (n) => {
  if (n === undefined || n === null) return '—'
  const u = ['B', 'KB', 'MB', 'GB', 'TB']
  let i = 0
  let v = n
  while (v >= 1024 && i < u.length - 1) { v /= 1024; i++ }
  return `${v.toFixed(v < 10 && i ? 1 : 0)} ${u[i]}`
}

// ------------------------------------------------------------------------------------------- status

const TONE = {
  ok: ['downloaded', 'running', 'online', 'healthy', 'succeeded', 'scanned', 'ok', 'up', 'enabled', 'on'],
  busy: ['scanning', 'downloading', 'installing', 'updating', 'queued', 'starting', 'deleting', 'restarting', 'building',
    'running_job', 're-uploading'],
  warn: ['uploaded', 'prepared', 'partial', 'interrupted', 'unknown', 'created', 'stopped'],
  info: ['available', 'update'],
  bad: ['failed', 'scan_failed', 'download_failed', 'offline', 'unhealthy', 'exited', 'dead', 'missing',
    'crashloop', 'error', 'conflict'],
}
export function toneOf(v) {
  if (TONE.ok.includes(v)) return 'ok'
  if (TONE.bad.includes(v)) return 'bad'
  if (TONE.busy.includes(v)) return 'warn busy'
  if (TONE.warn.includes(v)) return 'warn'
  if (TONE.info.includes(v)) return 'info'
  return 'muted'
}
export function Badge({ value, tone, title, children }) {
  const v = String(value ?? '—')
  const text = v.replace(/_/g, ' ')
  return <span className={`badge ${tone || toneOf(v)}`} title={title}>{children ?? text.charAt(0).toUpperCase() + text.slice(1)}</span>
}

export function Progress({ done = 0, total = 0, label, tone = '' }) {
  const pct = total ? Math.min(100, Math.round((100 * done) / total)) : 0
  return (
    <div className={`progress ${tone}`} role="progressbar" aria-valuenow={pct} aria-valuemin={0} aria-valuemax={100}>
      <div className="bar"><span style={{ width: `${pct}%` }} /></div>
      <span className="label">{label ?? `${pct}%`}</span>
    </div>
  )
}

// ------------------------------------------------------------------------------------------- layout

export function PageHeader({ title, description, actions, back, onBack }) {
  return (
    <header className="page-head">
      <div className="titles">
        {onBack && <a href="#" className="back" onClick={(e) => { e.preventDefault(); onBack() }}><ArrowLeft size={14} /> {back || 'Back'}</a>}
        <h1>{title}</h1>
        {description && <p className="desc">{description}</p>}
      </div>
      {actions && <div className="actions">{actions}</div>}
    </header>
  )
}

export function Card({ title, description, actions, children, className = '', flush, footer }) {
  return (
    <section className={`card ${flush ? 'flush' : ''} ${className}`}>
      {(title || actions) && (
        <header className="card-head">
          <div className="titles">
            <h2>{title}</h2>
            {description && <p className="desc">{description}</p>}
          </div>
          {actions && <div className="actions">{actions}</div>}
        </header>
      )}
      <div className="card-body">{children}</div>
      {footer && <footer className="card-foot">{footer}</footer>}
    </section>
  )
}

export function Tabs({ items, value, onChange, className = 'tabs' }) {
  return (
    <div className={className} role="tablist">
      {items.filter(Boolean).map((t) => (
        <button key={t.id} role="tab" aria-selected={value === t.id} className={value === t.id ? 'active' : ''} onClick={() => onChange(t.id)}>
          {t.icon && <t.icon size={15} />}{t.label}{t.count ? <span className="count">{t.count}</span> : null}
        </button>
      ))}
    </div>
  )
}

export function Field({ label, tag, hint, children }) {
  return (
    <label className="field">
      <span className="field-label">{label}{tag && <span className="tag">{tag}</span>}</span>
      {children}
      {hint && <span className="hint">{hint}</span>}
    </label>
  )
}

export function Msg({ error, ok, warnings, info }) {
  return (
    <>
      {error && <div className="msg bad" role="alert"><AlertCircle size={16} /><div>{error}</div></div>}
      {ok && <div className="msg ok" role="status"><CheckCircle2 size={16} /><div>{ok}</div></div>}
      {info && <div className="msg info"><Info size={16} /><div>{info}</div></div>}
      {warnings?.length > 0 && (
        <div className="msg warn"><AlertTriangle size={16} />
          <div>{warnings.map((w, i) => <div key={i}>{w}</div>)}</div>
        </div>
      )}
    </>
  )
}

export function Empty({ title, children, action }) {
  return (
    <div className="empty">
      {title && <b>{title}</b>}
      {children && <div>{children}</div>}
      {action && <div className="mt">{action}</div>}
    </div>
  )
}

export function Pager({ rows, size = 10, children }) {
  const [page, setPage] = useState(0)
  const pages = Math.max(1, Math.ceil(rows.length / size))
  const p = Math.min(page, pages - 1)
  return (
    <>
      {children(rows.slice(p * size, p * size + size))}
      {pages > 1 && (
        <div className="pager">
          <span>{p * size + 1}–{Math.min(rows.length, (p + 1) * size)} of {rows.length}</span>
          <button className="sm" disabled={p === 0} onClick={() => setPage(p - 1)}>Previous</button>
          <button className="sm" disabled={p >= pages - 1} onClick={() => setPage(p + 1)}>Next</button>
        </div>
      )}
    </>
  )
}

export function CopyButton({ text }) {
  const [done, setDone] = useState(false)
  return (
    <button className="sm" onClick={async () => {
      try { await navigator.clipboard.writeText(text); setDone(true); setTimeout(() => setDone(false), 1500) } catch { toast('Copy failed: select the text and copy it', 'bad') }
    }}>{done ? <><Check size={14} /> Copied</> : <><Copy size={14} /> Copy</>}</button>
  )
}

// ------------------------------------------------------------------------------------------- popovers

function useDismiss(open, setOpen) {
  const ref = useRef(null)
  useEffect(() => {
    if (!open) return undefined
    const down = (e) => ref.current && !ref.current.contains(e.target) && setOpen(false)
    const key = (e) => e.key === 'Escape' && setOpen(false)
    document.addEventListener('mousedown', down)
    document.addEventListener('keydown', key)
    return () => { document.removeEventListener('mousedown', down); document.removeEventListener('keydown', key) }
  }, [open, setOpen])
  return ref
}

function usePlacement(open, setOpen, anchor, align) {
  const [style, setStyle] = useState(null)
  useEffect(() => {
    if (!open) { setStyle(null); return undefined }
    const place = () => {
      const r = anchor.current?.getBoundingClientRect()
      if (!r) return
      const s = { position: 'fixed', top: 'auto', bottom: 'auto', left: 'auto', right: 'auto' }
      if (window.innerHeight - r.bottom < 280 && r.top > 280) s.bottom = window.innerHeight - r.top + 6
      else s.top = r.bottom + 6
      if (align === 'left') s.left = Math.max(8, r.left)
      else s.right = Math.max(8, window.innerWidth - r.right)
      setStyle(s)
    }
    place()
    const onScroll = (e) => { if (!anchor.current?.contains(e.target)) setOpen(false) }
    window.addEventListener('resize', place)
    window.addEventListener('scroll', onScroll, true)
    return () => { window.removeEventListener('resize', place); window.removeEventListener('scroll', onScroll, true) }
  }, [open, setOpen, anchor, align])
  return style
}

function focusItems(root, step) {
  const items = [...(root?.querySelectorAll('.menu-item:not(:disabled)') || [])]
  if (!items.length) return
  const i = items.indexOf(document.activeElement)
  items[(i + step + items.length) % items.length].focus()
}

export function Popover({ trigger, children, align = 'right', className = '' }) {
  const [open, setOpen] = useState(false)
  const ref = useDismiss(open, setOpen)
  const style = usePlacement(open, setOpen, ref, align)
  return (
    <div className="pop-anchor" ref={ref}>
      {trigger({ open, toggle: () => setOpen(!open) })}
      {open && style && <div className={`popover ${className}`} style={style}>{children(() => setOpen(false))}</div>}
    </div>
  )
}

export function Menu({ items, label = 'More actions', icon: Icon = MoreHorizontal, text, align = 'right', disabled, head }) {
  const [open, setOpen] = useState(false)
  const ref = useDismiss(open, setOpen)
  const style = usePlacement(open, setOpen, ref, align)
  const list = items.filter(Boolean)
  useEffect(() => { if (open && style) focusItems(ref.current, 1) }, [open, style, ref])
  if (!list.length) return null
  return (
    <div className="pop-anchor" ref={ref} onKeyDown={(e) => {
      if (e.key === 'ArrowDown') { e.preventDefault(); focusItems(ref.current, 1) }
      if (e.key === 'ArrowUp') { e.preventDefault(); focusItems(ref.current, -1) }
    }}>
      <button className={text ? '' : 'icon'} title={label} aria-label={label} aria-haspopup="menu" aria-expanded={open}
        disabled={disabled} onClick={(e) => { e.stopPropagation(); setOpen(!open) }}>
        <Icon size={16} />{text}{text && <ChevronDown size={14} />}
      </button>
      {open && style && (
        <div className="popover" style={style} role="menu" onClick={(e) => e.stopPropagation()}>
          {head}
          {list.map((it, i) => (it === '-' ? <div key={i} className="menu-sep" /> : (
            <button key={i} role="menuitem" className={`menu-item ${it.danger ? 'danger' : ''}`} disabled={it.disabled} title={it.title}
              onClick={() => { setOpen(false); it.onClick() }}>
              {it.icon && <it.icon size={15} />}{it.label}
            </button>
          )))}
        </div>
      )}
    </div>
  )
}

export function MultiSelect({ options, value, onChange, allLabel = 'All', noun = 'selected' }) {
  const label = !value.length ? allLabel : value.length === 1 ? value[0] : `${value.length} ${noun}`
  return (
    <Popover align="left" trigger={({ toggle, open }) => (
      <button className="ms-trigger" aria-expanded={open} onClick={toggle}><span>{label}</span><ChevronDown size={14} /></button>
    )}>{() => (
      <>
        <div className="row between center" style={{ padding: '4px 8px 6px' }}>
          <span className="muted small">{options.length} available</span>
          {value.length > 0 && <button className="ghost sm" onClick={() => onChange([])}>Clear</button>}
        </div>
        <div className="ms-list">
          {!options.length && <div className="muted small" style={{ padding: 8 }}>Nothing to choose yet</div>}
          {options.map((o) => (
            <label key={o} className="ms-item">
              <input type="checkbox" checked={value.includes(o)}
                onChange={() => onChange(value.includes(o) ? value.filter((x) => x !== o) : [...value, o])} />{o}
            </label>
          ))}
        </div>
      </>
    )}</Popover>
  )
}

// ------------------------------------------------------------------------------------------- modal + dialogs

export function Modal({ title, onClose, children, wide, footer }) {
  const box = useRef(null)
  const closeRef = useRef(onClose)
  closeRef.current = onClose
  useEffect(() => {
    const k = (e) => e.key === 'Escape' && closeRef.current()
    window.addEventListener('keydown', k)
    const prev = document.activeElement
    const first = box.current?.querySelector('input:not([type=hidden]):not(:disabled), select, textarea, .modal-foot button:not(:disabled)')
    ;(first || box.current)?.focus()
    return () => { window.removeEventListener('keydown', k); prev?.focus?.() }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])
  return (
    <div className="modal-bg" onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <div className={`modal ${wide ? 'wide' : ''}`} role="dialog" aria-modal="true" aria-label={typeof title === 'string' ? title : undefined}
        ref={box} tabIndex={-1}>
        <header className="modal-head">
          <h3>{title}</h3>
          <button className="icon" onClick={onClose} title="Close" aria-label="Close"><X size={16} /></button>
        </header>
        {children && <div className="modal-body">{children}</div>}
        {footer && <footer className="modal-foot">{footer}</footer>}
      </div>
    </div>
  )
}

let pushDialog = null
let pushToast = null

/** In-app confirm. opts: title, message, confirmLabel, danger, typeToConfirm. Resolves true / false. */
export function confirmDialog(opts) {
  const o = typeof opts === 'string' ? { title: opts } : opts
  return new Promise((res) => (pushDialog ? pushDialog({ kind: 'confirm', ...o, res }) : res(window.confirm(o.message || o.title))))
}

/** In-app prompt. opts: title, message, label, type, placeholder, minLength, confirmLabel, danger. Resolves the text or null. */
export function promptDialog(opts) {
  return new Promise((res) => (pushDialog ? pushDialog({ kind: 'prompt', ...opts, res }) : res(window.prompt(opts.message || opts.title))))
}

export function toast(message, tone = 'ok') {
  if (pushToast) pushToast({ message: String(message), tone })
  else if (tone === 'bad') window.alert(message)
}

function DialogView({ d, close }) {
  const [text, setText] = useState('')
  const needs = d.typeToConfirm
  const okDisabled = d.kind === 'prompt' ? text.length < (d.minLength || 1) : needs ? text !== needs : false
  const done = (v) => { d.res(v); close() }
  const submit = () => !okDisabled && done(d.kind === 'prompt' ? text : true)
  return (
    <Modal title={d.title} onClose={() => done(d.kind === 'prompt' ? null : false)}
      footer={<>
        <button onClick={() => done(d.kind === 'prompt' ? null : false)}>Cancel</button>
        <button className={d.danger ? 'danger solid' : 'primary'} disabled={okDisabled} onClick={submit}>{d.confirmLabel || 'Confirm'}</button>
      </>}>
      {(d.message || d.kind === 'prompt' || needs) ? <>
      {d.message && <p className="dialog-text">{d.message}</p>}
      {(d.kind === 'prompt' || needs) && (
        <form onSubmit={(e) => { e.preventDefault(); submit() }}>
          <Field label={d.label || (needs ? <>Type <code>{needs}</code> to confirm</> : 'Value')}
            hint={d.minLength ? `At least ${d.minLength} characters` : undefined}>
            <input type={d.type || 'text'} value={text} onChange={(e) => setText(e.target.value)} placeholder={d.placeholder}
              autoComplete="off" />
          </Field>
        </form>
      )}
      </> : null}
    </Modal>
  )
}

export function OverlayHost() {
  const [dialog, setDialog] = useState(null)
  const [toasts, setToasts] = useState([])
  useEffect(() => {
    let n = 0
    pushDialog = (d) => setDialog({ ...d, id: ++n })
    pushToast = (t) => {
      const id = ++n
      setToasts((x) => [...x.slice(-3), { ...t, id }])
      setTimeout(() => setToasts((x) => x.filter((y) => y.id !== id)), t.tone === 'bad' ? 9000 : 4500)
    }
    return () => { pushDialog = null; pushToast = null }
  }, [])
  const Icon = { ok: CheckCircle2, bad: AlertCircle, info: Info }
  return (
    <>
      {dialog && <DialogView key={dialog.id} d={dialog} close={() => setDialog(null)} />}
      <div className="toasts" aria-live="polite">
        {toasts.map((t) => {
          const I = Icon[t.tone] || Info
          return (
            <div key={t.id} className={`toast ${t.tone}`} role={t.tone === 'bad' ? 'alert' : 'status'}>
              <I size={16} /><div>{t.message}</div>
              <button className="icon" aria-label="Dismiss" onClick={() => setToasts((x) => x.filter((y) => y.id !== t.id))}><X size={14} /></button>
            </div>
          )
        })}
      </div>
    </>
  )
}
