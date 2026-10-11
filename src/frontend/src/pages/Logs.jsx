import { useEffect, useRef, useState } from 'react'
import { Download, LifeBuoy, Pause, Play, Search } from 'lucide-react'
import { api, can, download, qs } from '../api.js'
import { Card, Field, Menu, Msg, MultiSelect, PageHeader, useAction, usePoll } from '../ui.jsx'

const LEVELS = ['', 'DEBUG', 'INFO', 'WARN', 'ERROR']
const toIso = (local) => (local ? new Date(local).toISOString() : '')
const hoursAgo = (h) => {
  const d = new Date(Date.now() - h * 3600e3)
  d.setMinutes(d.getMinutes() - d.getTimezoneOffset())
  return d.toISOString().slice(0, 16)
}

function Line({ r }) {
  return (
    <div className={`logline lvl-${r.lvl}`}>
      <span className="ts">{new Date(r.ts).toLocaleString()}</span>
      <span className="lvl">{r.lvl}</span>
      <span className="src">{r.node}/{r.svc}</span>
      <span className="text">{r.msg}</span>
    </div>
  )
}

export default function Logs() {
  const [svcs, , svcsError] = usePoll(() => api('/api/logs/services'), 30000)
  const [node, setNode] = useState('')
  const [services, setServices] = useState([])
  const [level, setLevel] = useState('')
  const [q, setQ] = useState('')
  const [from, setFrom] = useState(hoursAgo(1))
  const [to, setTo] = useState('')
  const [entries, setEntries] = useState([])
  const [searched, setSearched] = useState(false)
  const [live, setLive] = useState(false)
  const [error, setError] = useState('')
  const [busy, run] = useAction(setError)
  const seq = useRef(0)
  const box = useRef(null)
  const nodes = Object.keys(svcs?.nodes || {})
  const allSvcs = [...new Set(Object.entries(svcs?.nodes || {}).filter(([n]) => !node || n === node).flatMap(([, s]) => s))].sort()
  const filter = { node, service: services.join(','), level, q }

  const search = () => run(async () => {
    setLive(false)
    const r = await api(`/api/logs?${qs({ ...filter, from: toIso(from), to: toIso(to), limit: 5000 })}`)
    setEntries(r.entries)
    setSearched(true)
    setError('')
  })

  useEffect(() => {
    if (!live) return undefined
    let stop = false
    let inflight = false
    const tick = async () => {
      if (inflight) return
      inflight = true
      try {
        const r = await api(`/api/logs/tail?${qs({ ...filter, after: seq.current, limit: 1000 })}`)
        if (stop) return
        if (seq.current === 0 || r.seq < seq.current) setEntries(r.entries.slice(-500))
        else if (r.entries.length) setEntries((e) => [...e, ...r.entries].slice(-3000))
        seq.current = r.seq
        setError('')
      } catch (e) {
        if (!stop) setError(e.message)
      } finally {
        inflight = false
      }
    }
    seq.current = 0
    tick()
    const t = setInterval(tick, 2000)
    return () => { stop = true; clearInterval(t) }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [live, node, services.join(), level, q])

  useEffect(() => { if (box.current) box.current.scrollTop = box.current.scrollHeight }, [entries])

  const dl = (format, all) => run(() => download(`/api/logs/download?${qs(all ? { from: toIso(from), to: toIso(to), format: 'zip' }
    : { ...filter, from: toIso(from), to: toIso(to), format })}`, `logs.${format}`))
  const bundle = (hours) => run(() => download(`/api/support-bundle?hours=${hours}`, 'support.zip'))

  return (
    <div className="stack">
      <PageHeader title="Logs" description="Logs of every pod on every node, collected by sm-logger. Search a time range or follow them live."
        actions={can('operator') && <>
          <Menu text="Download" icon={Download} label="Download logs" disabled={busy} items={[
            { label: 'Filtered lines (.log)', icon: Download, onClick: () => dl('log') },
            { label: 'Filtered lines (.zip)', icon: Download, onClick: () => dl('zip') },
            { label: 'All logs in the time range (.zip)', icon: Download, onClick: () => dl('zip', true) },
          ]} />
          <Menu text="Support bundle" icon={LifeBuoy} label="Download support bundle" disabled={busy}
            head={<div className="menu-head"><span className="muted small">Logs, state and config with secrets masked</span></div>}
            items={[6, 24, 72, 168].map((h) => ({ label: h < 168 ? `Last ${h} hours` : 'Last 7 days', onClick: () => bundle(h) }))} />
        </>} />
      <Card>
        <div className="log-toolbar">
          <Field label="Node"><select value={node} onChange={(e) => setNode(e.target.value)}>
            <option value="">All nodes</option>{nodes.map((n) => <option key={n}>{n}</option>)}</select></Field>
          <Field label="Services"><MultiSelect options={allSvcs} value={services} onChange={setServices} allLabel="All services" noun="services" /></Field>
          <Field label="Minimum level"><select value={level} onChange={(e) => setLevel(e.target.value)}>
            {LEVELS.map((l) => <option key={l} value={l}>{l || 'All levels'}</option>)}</select></Field>
          <div className="grow" style={{ minWidth: 200 }}>
            <Field label="Contains"><input value={q} onChange={(e) => setQ(e.target.value)} placeholder="Text to find"
              onKeyDown={(e) => e.key === 'Enter' && search()} /></Field>
          </div>
          <Field label="From"><input type="datetime-local" value={from} onChange={(e) => setFrom(e.target.value)} disabled={live} /></Field>
          <Field label="To"><input type="datetime-local" value={to} onChange={(e) => setTo(e.target.value)} disabled={live} /></Field>
          <div className="row gap">
            <button className="primary" disabled={busy} onClick={search}><Search size={15} /> Search</button>
            <button className={live ? 'on' : ''} onClick={() => setLive(!live)} aria-pressed={live}>
              {live ? <><Pause size={15} /> Stop live</> : <><Play size={15} /> Live</>}</button>
          </div>
        </div>
        <Msg error={error || svcsError} />
      </Card>
      <Card flush title={<>{live && <span className="live-dot" aria-hidden="true" />}{entries.length.toLocaleString()} lines</>}
        description={live ? 'Following new lines every 2 seconds (newest at the bottom)' : searched ? 'Search result (newest at the bottom)' : undefined}>
        <div className="logbox" ref={box} role="log" aria-live={live ? 'polite' : 'off'}>
          {entries.length === 0
            ? <div style={{ color: '#a1a1aa', padding: 8 }}>{searched ? 'No log lines match these filters.' : 'Search a time range or start Live to see log lines.'}</div>
            : entries.map((r, i) => <Line key={(r.seq || '') + '-' + i} r={r} />)}
        </div>
      </Card>
    </div>
  )
}
