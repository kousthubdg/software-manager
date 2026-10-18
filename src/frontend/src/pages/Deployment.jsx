import { useEffect, useMemo, useState } from 'react'
import {
  Database, Edit3, FileText, KeyRound, Play, Plus, RotateCcw, RotateCw, Square, Trash2, Upload,
} from 'lucide-react'
import { api, can } from '../api.js'
import {
  Badge, Card, Empty, Field, Menu, Modal, Msg, PageHeader, Pager, Progress, confirmDialog, fmtBytes, fmtTime, toast,
  useAction, usePoll,
} from '../ui.jsx'

// ------------------------------------------------------------------------------------------- map editor

function MapEditor({ version, initial, depId, alarmBase, setAlarmBase, onSubmit, submitLabel, busy, nameField, onCancel }) {
  const [nodes] = usePoll(() => api('/api/nodes'), 0)
  const [rel] = usePoll(() => (version ? api(`/api/releases/${encodeURIComponent(version)}`) : Promise.resolve(null)), 0, [version])
  const [rows, setRows] = useState(initial?.nodes?.length ? initial.nodes : [{ node_id: '', pods: [] }])
  const [overrides, setOverrides] = useState(initial?.overrides || {})
  const [check, setCheck] = useState(null)
  const pods = useMemo(() => (rel?.manifest?.installers || []).filter((i) => !i.self_update).map((i) => i.name), [rel])
  const used = new Set(rows.flatMap((r) => r.pods))
  const map = { nodes: rows.filter((r) => r.node_id).map((r) => ({ node_id: Number(r.node_id), pods: r.pods })), overrides }

  useEffect(() => {
    if (!version) return undefined
    const t = setTimeout(() => {
      api('/api/deployments/validate', { method: 'POST', body: { version, map, alarm_base_path: alarmBase, id: depId } })
        .then(setCheck).catch((e) => setCheck({ ok: false, errors: [e.message], warnings: [] }))
    }, 400)
    return () => clearTimeout(t)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [version, JSON.stringify(map), alarmBase])

  const setRow = (i, patch) => setRows(rows.map((r, j) => (j === i ? { ...r, ...patch } : r)))
  const toggle = (i, pod) => {
    const r = rows[i]
    setRow(i, { pods: r.pods.includes(pod) ? r.pods.filter((p) => p !== pod) : [...r.pods, pod] })
  }
  const setOv = (pod, k, v) => setOverrides({ ...overrides, [pod]: { ...(overrides[pod] || {}), [k]: v || undefined } })
  const resolved = rel?.resolved || {}

  return (
    <div className="stack">
      {(nameField || setAlarmBase) && (
        <div className="form-grid">
          {nameField}
          {setAlarmBase && (
            <Field label="Alarm base path" hint="Must be under the data root. Becomes {{ alarm_base_path }}.">
              <input value={alarmBase} onChange={(e) => setAlarmBase(e.target.value)} />
            </Field>
          )}
        </div>
      )}
      <div>
        <div className="row between center wrap gap" style={{ marginBottom: 8 }}>
          <div>
            <h3 style={{ margin: 0 }}>Pods per node</h3>
            <p className="muted small">{used.size} of {pods.length} pods placed. Each pod runs on exactly one node.</p>
          </div>
          <div className="row gap">
            <button className="sm" onClick={() => setRows([{ node_id: rows[0]?.node_id || nodes?.[0]?.id || '', pods }])}>All pods on one node</button>
            <button className="sm" onClick={() => setRows([...rows, { node_id: '', pods: [] }])}><Plus size={14} /> Add node</button>
          </div>
        </div>
        <div className="stack-sm">
          {rows.map((r, i) => (
            <div key={i} className="map-row">
              <select value={r.node_id} onChange={(e) => setRow(i, { node_id: e.target.value })} aria-label="Node">
                <option value="">Select a node…</option>
                {nodes?.map((n) => <option key={n.id} value={n.id}>{n.name} ({n.ip}){n.status !== 'online' ? ` · ${n.status}` : ''}</option>)}
              </select>
              <div className="pod-pick">
                {pods.map((p) => (
                  <label key={p} className={`pick ${r.pods.includes(p) ? 'on' : used.has(p) ? 'taken' : ''}`}>
                    <input type="checkbox" checked={r.pods.includes(p)} disabled={!r.pods.includes(p) && used.has(p)}
                      onChange={() => toggle(i, p)} />{p}
                  </label>
                ))}
                {!pods.length && <span className="muted small">Loading pods…</span>}
              </div>
              <button className="icon danger" title="Remove this node row" aria-label="Remove this node row"
                onClick={() => setRows(rows.filter((_, j) => j !== i))}><Trash2 size={15} /></button>
            </div>
          ))}
        </div>
      </div>
      <details>
        <summary>Advanced: per-pod port and resource overrides</summary>
        <div style={{ overflowX: 'auto' }}>
          <table className="tbl compact">
            <thead><tr><th>Pod</th><th>Default port</th><th>Port</th><th>Memory limit</th><th>CPUs</th><th>GPU</th></tr></thead>
            <tbody>
              {pods.map((p) => (
                <tr key={p}>
                  <td>{p}</td>
                  <td className="num">{resolved[p]?.port ?? '—'}</td>
                  <td><input type="number" aria-label={`${p} port`} value={overrides[p]?.service_port || ''} onChange={(e) => setOv(p, 'service_port', e.target.value ? Number(e.target.value) : '')} style={{ width: 96 }} /></td>
                  <td><input aria-label={`${p} memory limit`} value={overrides[p]?.mem_limit || ''} onChange={(e) => setOv(p, 'mem_limit', e.target.value)} placeholder="e.g. 4g" style={{ width: 96 }} /></td>
                  <td><input aria-label={`${p} CPUs`} value={overrides[p]?.cpus || ''} onChange={(e) => setOv(p, 'cpus', e.target.value)} placeholder="e.g. 2" style={{ width: 76 }} /></td>
                  <td><input type="checkbox" aria-label={`${p} GPU`} checked={!!overrides[p]?.gpu} onChange={(e) => setOv(p, 'gpu', e.target.checked)} /></td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </details>
      {check && <Msg error={check.errors?.join('\n')} ok={check.ok && !check.warnings?.length && 'The map is valid.'} warnings={check.warnings} />}
      <div className="row gap">
        <button className="primary" disabled={busy || !check?.ok} onClick={() => onSubmit(map)}>{submitLabel}</button>
        {onCancel && <button onClick={onCancel}>Cancel</button>}
      </div>
    </div>
  )
}

function AddDeployment({ onDone, onCancel }) {
  const [rels] = usePoll(() => api('/api/releases'), 0)
  const downloaded = (rels || []).filter((r) => r.status === 'downloaded')
  const names = [...new Set(downloaded.map((r) => r.software_name))]
  const [sw, setSw] = useState('')
  const [version, setVersion] = useState('')
  const [name, setName] = useState('main')
  const [alarmBase, setAlarmBase] = useState('')
  const [error, setError] = useState('')
  const [busy, run] = useAction(setError)
  useEffect(() => { if (!sw && names.length) setSw(names[0]) }, [names.join()])  // eslint-disable-line
  useEffect(() => {
    api('/api/settings').then((s) => setAlarmBase((a) => a || (s.data_root ? `${s.data_root.replace(/\/+$/, '')}/alarms` : '')))
      .catch(() => {})
  }, [])
  const versions = downloaded.filter((r) => r.software_name === sw)

  return (
    <Card title="New deployment" description="Choose a stored release, place its pods on nodes, then install.">
      {rels && !downloaded.length ? <Empty title="No stored release yet">Add one on the Releases page first.</Empty> : (
        <div className="stack">
          <div className="form-grid">
            <Field label="Software">
              <select value={sw} onChange={(e) => { setSw(e.target.value); setVersion('') }}>
                {names.map((n) => <option key={n}>{n}</option>)}
              </select>
            </Field>
            <Field label="Version" hint="Stored releases only">
              <select value={version} onChange={(e) => setVersion(e.target.value)}>
                <option value="">Select a version…</option>
                {versions.map((r) => <option key={r.version}>{r.version}</option>)}
              </select>
            </Field>
          </div>
          {version ? (
            <MapEditor version={version} alarmBase={alarmBase} setAlarmBase={setAlarmBase} busy={busy} submitLabel="Install" onCancel={onCancel}
              nameField={<Field label="Deployment name"><input value={name} onChange={(e) => setName(e.target.value)} /></Field>}
              onSubmit={(map) => run(async () => {
                const r = await api('/api/deployments', { method: 'POST', body: { name, version, map, alarm_base_path: alarmBase } })
                toast(`Deployment ${name} created; installing`)
                onDone(r.id)
              })} />
          ) : <div><button onClick={onCancel}>Cancel</button></div>}
        </div>
      )}
      <Msg error={error} />
    </Card>
  )
}

// ------------------------------------------------------------------------------------------- jobs

export function JobModal({ id, onClose }) {
  const [job, reload] = usePoll(() => api(`/api/jobs/${id}`), 2000, [id])
  const [busy, run] = useAction()
  const canResume = job && ['failed', 'interrupted'].includes(job.status) && can('operator') &&
    !job.steps.some((s) => String(s.id).startsWith('c'))
  return (
    <Modal title={job ? `Job #${job.id} · ${job.kind}` : 'Job'} onClose={onClose} wide
      footer={<>
        {canResume && <button className="primary" disabled={busy}
          onClick={() => run(async () => { await api(`/api/jobs/${id}/resume`, { method: 'POST' }); reload() })}>Resume from failed step</button>}
        <button onClick={onClose}>Close</button>
      </>}>
      {!job ? <Empty>Loading…</Empty> : (
        <>
          <div className="row gap center wrap">
            <Badge value={job.status} />
            <div style={{ width: 220 }}><Progress done={job.progress.done} total={job.progress.total} label={`${job.progress.done}/${job.progress.total} steps`} /></div>
            <span className="muted small">by {job.created_by} · {fmtTime(job.created_at)}</span>
          </div>
          <div style={{ overflowX: 'auto' }}>
            <table className="tbl compact">
              <thead><tr><th>#</th><th>Step</th><th>Pod / node</th><th>Status</th><th>Output</th></tr></thead>
              <tbody>
                {job.steps.map((s) => (
                  <tr key={s.id}>
                    <td className="num muted">{s.id}</td><td>{s.kind}</td><td>{s.pod || s.node_name || ''}</td>
                    <td><Badge value={s.status === 'ok' ? 'succeeded' : s.status} /></td>
                    <td className="wrap-text small">{s.output}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <details open={job.status === 'failed'}>
            <summary>Job log</summary>
            <pre className="pre">{job.log}</pre>
          </details>
        </>
      )}
    </Modal>
  )
}

function Jobs({ depId }) {
  const [jobs] = usePoll(() => api(`/api/jobs?deployment_id=${depId}&limit=50`), 3000, [depId])
  const [open, setOpen] = useState(null)
  return (
    <Card flush title="Jobs" description="Every action runs as a job. Click one for its steps and log.">
      {!jobs?.length ? <Empty>{jobs ? 'No jobs yet.' : 'Loading…'}</Empty> : (
        <Pager rows={jobs} size={8}>{(rows) => (
          <table className="tbl hover compact">
            <thead><tr><th>#</th><th>Kind</th><th>Status</th><th>Progress</th><th>By</th><th>Started</th></tr></thead>
            <tbody>{rows.map((j) => (
              <tr key={j.id} onClick={() => setOpen(j.id)} tabIndex={0} onKeyDown={(e) => e.key === 'Enter' && setOpen(j.id)}>
                <td className="num muted">{j.id}</td><td>{j.kind}</td><td><Badge value={j.status} /></td>
                <td className="num">{j.progress.done}/{j.progress.total}</td><td>{j.created_by}</td><td>{fmtTime(j.started_at || j.created_at)}</td>
              </tr>
            ))}</tbody>
          </table>
        )}</Pager>
      )}
      {open && <JobModal id={open} onClose={() => setOpen(null)} />}
    </Card>
  )
}

// ------------------------------------------------------------------------------------------- pod modals

function LogsModal({ pod, onClose }) {
  const [tail, setTail] = useState(300)
  const [text, reload, error] = usePoll(() => api(`/api/nodes/${pod.node_id}/pods/${encodeURIComponent(pod.name)}/logs?tail=${tail}`), 4000, [tail])
  return (
    <Modal title={`Logs · ${pod.name} on ${pod.node}`} onClose={onClose} wide
      footer={<>
        <select value={tail} onChange={(e) => setTail(Number(e.target.value))} aria-label="Lines">
          {[100, 300, 1000, 3000].map((n) => <option key={n} value={n}>Last {n} lines</option>)}</select>
        <button onClick={reload}><RotateCw size={15} /> Refresh</button>
        <button onClick={onClose}>Close</button>
      </>}>
      <Msg error={error} />
      <pre className="pre logs" ref={(el) => el && (el.scrollTop = el.scrollHeight)}>{text ?? (error ? '' : 'Loading…')}</pre>
    </Modal>
  )
}

function ValuesModal({ pod, onClose }) {
  const valuesPath = `/api/nodes/${pod.node_id}/pods/${encodeURIComponent(pod.name)}/values`
  const [data, reload, loadError] = usePoll(() => api(valuesPath), 0)
  const [edits, setEdits] = useState({})
  const [secretKey, setSecretKey] = useState('')
  const [secretVal, setSecretVal] = useState('')
  const [error, setError] = useState('')
  const [busy, run] = useAction(setError)
  const dirty = Object.keys(edits).length > 0 || !!secretKey
  const save = () => run(async () => {
    const secrets = secretKey ? { [secretKey]: secretVal } : {}
    await api(valuesPath, { method: 'PUT', body: { values: edits, secrets } })
    toast(`Saved values of ${pod.name}. Restart the pod to apply.`)
    setEdits({})
    setSecretKey('')
    setSecretVal('')
    reload()
  })
  return (
    <Modal title={`values.env · ${pod.name}`} onClose={onClose} wide
      footer={<>
        {dirty && <span className="muted small grow">Unsaved changes</span>}
        <button onClick={onClose}>{dirty ? 'Discard' : 'Close'}</button>
        {can('admin') && <button className="primary" disabled={busy || !dirty} onClick={save}>Save</button>}
      </>}>
      <Msg info="Managed keys (DB_IP, MQTT_HOST, *_URL, ports, TZ, SYS_IP) are rewritten by Software Manager on every start. Other keys are kept across upgrades. Secret values are never shown." />
      {!data ? (loadError ? null : <Empty>Loading…</Empty>) : (
        <table className="tbl compact">
          <thead><tr><th style={{ width: '35%' }}>Key</th><th>Value</th></tr></thead>
          <tbody>
            {Object.entries(data.values || {}).map(([k, v]) => (
              <tr key={k}><td><span className="mono small">{k}</span></td><td>
                {v === '***' || !can('admin') ? <span className="muted">{v}</span> :
                  <input aria-label={k} value={edits[k] ?? v} onChange={(e) => setEdits({ ...edits, [k]: e.target.value })} style={{ width: '100%' }} />}
              </td></tr>
            ))}
          </tbody>
        </table>
      )}
      {can('admin') && data && (data.secrets || []).length > 0 && (
        <div className="form-grid">
          <Field label="Set a secret"><select value={secretKey} onChange={(e) => setSecretKey(e.target.value)}>
            <option value="">Select a secret…</option>{(data.secrets || []).map((s) => <option key={s}>{s}</option>)}</select></Field>
          <Field label="New value"><input type="password" value={secretVal} disabled={!secretKey} onChange={(e) => setSecretVal(e.target.value)} autoComplete="off" /></Field>
        </div>
      )}
      <Msg error={error || loadError} />
    </Modal>
  )
}

// ------------------------------------------------------------------------------------------- detail

function UpdateModal({ dep, onClose, onJob }) {
  const [rels] = usePoll(() => api('/api/releases'), 0)
  const options = (rels || []).filter((r) => r.status === 'downloaded' && r.software_name === dep.software_name && r.version !== dep.version)
  const [version, setVersion] = useState('')
  const [diff, setDiff] = useState(null)
  const [error, setError] = useState('')
  const [busy, run] = useAction(setError)
  useEffect(() => {
    let stale = false
    setDiff(null)
    setError('')
    if (version) {
      api(`/api/releases/${encodeURIComponent(version)}/diff?frm=${encodeURIComponent(dep.version)}`)
        .then((d) => { if (!stale) setDiff(d) }).catch((e) => { if (!stale) setError(e.message) })
    }
    return () => { stale = true }
  }, [version, dep.version])
  return (
    <Modal title={`Update ${dep.name}`} onClose={onClose}
      footer={<>
        <button onClick={onClose}>Cancel</button>
        <button className="primary" disabled={busy || !version}
          onClick={() => run(async () => { const r = await api(`/api/deployments/${dep.id}/update`, { method: 'POST', body: { version } }); onJob(r.job) })}>
          {version ? `Update to ${version}` : 'Update'}</button>
      </>}>
      <p className="dialog-text">Currently running <b>{dep.version}</b>.</p>
      {rels && !options.length ? <Empty title="Nothing to update to">No other stored release of {dep.software_name}.</Empty> : (
        <Field label="New version"><select value={version} onChange={(e) => setVersion(e.target.value)}>
          <option value="">Select a stored release…</option>{options.map((r) => <option key={r.version}>{r.version}</option>)}</select></Field>
      )}
      {diff && (
        <>
          <div className="kv" style={{ marginBottom: 0 }}>
            <div><b>Changed</b> {diff.changed.join(', ') || 'None'}</div>
            <div><b>Added</b> {diff.added.join(', ') || 'None'}</div>
            <div><b>Removed</b> {diff.removed.join(', ') || 'None'}</div>
            <div><b>Unchanged</b> {diff.unchanged.length} pods, not restarted</div>
          </div>
          <Msg info="The database is backed up first (pg_dump). If a changed pod fails its health check, already-updated pods roll back automatically." />
        </>
      )}
      <Msg error={error} />
    </Modal>
  )
}

function ApplyChangesModal({ dep, onClose, onJob }) {
  const ch = dep.release_changes
  const [error, setError] = useState('')
  const [busy, run] = useAction(setError)
  const apply = (map) => run(async () => {
    const r = await api(`/api/deployments/${dep.id}/update`, { method: 'POST', body: { version: dep.version, ...(map ? { map } : {}) } })
    onJob(r.job)
  })
  const initial = { ...dep.map, nodes: (dep.map?.nodes || []).map((n) => ({ ...n, pods: n.pods.filter((p) => !ch.removed.includes(p)) })) }
  return (
    <Modal title={`Apply changes of ${dep.version} · ${dep.name}`} onClose={onClose} wide={ch.added.length > 0}
      footer={!ch.added.length && <>
        <button onClick={onClose}>Cancel</button>
        <button className="primary" disabled={busy} onClick={() => apply(null)}>Apply changes</button>
      </>}>
      <p className="dialog-text">Release {dep.version} was re-uploaded {fmtTime(ch.uploaded_at)}{ch.uploaded_by ? ` by ${ch.uploaded_by}` : ''}.
        Only these pods are touched; the others keep running.</p>
      <div className="kv" style={{ marginBottom: 0 }}>
        <div><b>Changed</b> {ch.changed.join(', ') || 'None'}</div>
        <div><b>Added</b> {ch.added.join(', ') || 'None'}</div>
        <div><b>Removed</b> {ch.removed.join(', ') || 'None'}</div>
      </div>
      <Msg info="The database is backed up first (pg_dump). The image tag stays the same, so a changed pod that fails its health check is not rolled back automatically; added pods are removed again." />
      {ch.added.length > 0 && <>
        <p className="dialog-text">Choose a node for each added pod:</p>
        <MapEditor version={dep.version} initial={initial} depId={dep.id} alarmBase={dep.alarm_base_path} busy={busy}
          submitLabel="Apply changes" onCancel={onClose} onSubmit={apply} />
      </>}
      <Msg error={error} />
    </Modal>
  )
}

function DeleteModal({ dep, onClose, onJob }) {
  const [confirm, setConfirm] = useState('')
  const [removeData, setRemoveData] = useState(false)
  const [confirmData, setConfirmData] = useState('')
  const [error, setError] = useState('')
  const [busy, run] = useAction(setError)
  return (
    <Modal title={`Delete deployment ${dep.name}?`} onClose={onClose}
      footer={<>
        <button onClick={onClose}>Cancel</button>
        <button className="danger solid" disabled={busy || confirm !== dep.name || (removeData && confirmData !== 'DELETE DATA')}
          onClick={() => run(async () => {
            const r = await api(`/api/deployments/${dep.id}/delete`, { method: 'POST', body: { confirm_name: confirm, remove_data: removeData, confirm_data: confirmData } })
            onJob(r.job)
          })}>Delete deployment</button>
      </>}>
      <p className="dialog-text">Stops and removes every pod of this deployment on all nodes, plus their images.</p>
      <Field label={<>Type <code>{dep.name}</code> to confirm</>}><input value={confirm} onChange={(e) => setConfirm(e.target.value)} autoComplete="off" /></Field>
      <label className="check"><input type="checkbox" checked={removeData} onChange={(e) => setRemoveData(e.target.checked)} />
        Also delete pod data (database, configs, recordings)</label>
      {removeData && <Field label={<>Type <code>DELETE DATA</code></>}><input value={confirmData} onChange={(e) => setConfirmData(e.target.value)} autoComplete="off" /></Field>}
      <Msg error={error} />
    </Modal>
  )
}

function Backups({ dep, onJob }) {
  const [list, reload] = usePoll(() => api(`/api/deployments/${dep.id}/backups`), 10000, [dep.id])
  const [error, setError] = useState('')
  const [busy, run] = useAction(setError)
  if (!dep.pods.some((p) => p.name === 'postgres-database')) return null
  return (
    <Card flush title="Database backups" description="One is taken automatically before every update."
      actions={can('operator') && <button disabled={busy} onClick={() => run(async () => {
        const r = await api(`/api/deployments/${dep.id}/backup`, { method: 'POST' }); onJob(r.job); setTimeout(reload, 5000)
      })}><Database size={15} /> Back up now</button>}>
      <Msg error={error} />
      {!list?.length ? <Empty>{list ? 'No backups yet.' : 'Loading…'}</Empty> : (
        <table className="tbl compact">
          <thead><tr><th>Backup</th><th>Version</th><th>Size</th><th /></tr></thead>
          <tbody>{list.map((b) => (
            <tr key={b.id}><td><span className="mono small">{b.id}</span></td><td>{b.version}</td><td className="num">{fmtBytes(b.size)}</td>
              <td className="actions">{can('admin') && <button className="sm danger" disabled={busy} onClick={async () => {
                if (!await confirmDialog({ title: `Restore ${b.id}?`, message: 'The current database is replaced by this backup.', typeToConfirm: dep.name, confirmLabel: 'Restore', danger: true })) return
                run(async () => { const r = await api(`/api/deployments/${dep.id}/restore`, { method: 'POST', body: { backup: b.id, confirm_name: dep.name } }); onJob(r.job) })
              }}>Restore</button>}</td></tr>
          ))}</tbody>
        </table>
      )}
    </Card>
  )
}

function Wiring({ id }) {
  const [wiring, setWiring] = useState(null)
  const [open, setOpen] = useState(false)
  const [error, setError] = useState('')
  const toggle = () => {
    setOpen(!open)
    if (!wiring) api(`/api/deployments/${id}/wiring`).then(setWiring).catch((e) => setError(e.message))
  }
  return (
    <Card flush title="Wiring" description="Managed environment computed from the map (addresses and ports between pods)."
      actions={<button className="sm" onClick={toggle}>{open ? 'Hide' : 'Show'}</button>}>
      <Msg error={error} />
      {open && (wiring ? (
        <table className="tbl compact">
          <thead><tr><th>Pod</th><th>Key</th><th>Value</th></tr></thead>
          <tbody>{Object.entries(wiring).flatMap(([pod, env]) => Object.entries(env).map(([k, v]) => (
            <tr key={pod + k}><td>{pod}</td><td><span className="mono small">{k}</span></td><td><span className="mono small">{v}</span></td></tr>
          )))}</tbody>
        </table>
      ) : !error && <Empty>Loading…</Empty>)}
    </Card>
  )
}

function DeploymentDetail({ id, onBack }) {
  const [dep, reload, loadError] = usePoll(() => api(`/api/deployments/${id}`), 4000, [id])
  const [modal, setModal] = useState(null)
  const [job, setJob] = useState(null)
  const [busy, run] = useAction()
  if (!dep) {
    return <div className="stack"><PageHeader title="Deployment" onBack={onBack} back="Deployments" />
      <Card>{loadError ? <Msg error={loadError} /> : <Empty>Loading…</Empty>}</Card></div>
  }
  const startJob = (j) => { setModal(null); setJob(j); reload() }
  const ask = { stop: 'Stop', restart: 'Restart', rollback: 'Roll back' }
  const why = {
    stop: 'Stopped pods stay down until you start them again; their services are unavailable meanwhile.',
    restart: 'Each pod restarts; its service is briefly unavailable.',
    rollback: 'The pod goes back to its previous version.',
  }
  const act = async (action) => {
    if (ask[action] && !await confirmDialog({ title: `${ask[action]} every pod of ${dep.name}?`, message: why[action], confirmLabel: ask[action], danger: action === 'stop' })) return
    run(async () => startJob((await api(`/api/deployments/${id}/action`, { method: 'POST', body: { action } })).job))
  }
  const podAct = async (pod, action) => {
    if (ask[action] && !await confirmDialog({ title: `${ask[action]} ${pod}?`, message: why[action], confirmLabel: ask[action], danger: action === 'stop' })) return
    run(async () => startJob((await api(`/api/deployments/${id}/pods/${encodeURIComponent(pod)}/action`, { method: 'POST', body: { action } })).job))
  }
  const locked = busy || !!dep.busy_job
  const healthy = dep.pods.filter((p) => p.status?.state === 'running' && p.status?.health !== 'unhealthy').length

  return (
    <div className="stack">
      <PageHeader onBack={onBack} back="Deployments"
        title={<>{dep.name} <Badge value={dep.state} /></>}
        description={`${dep.software_name} ${dep.version} · ${healthy} of ${dep.pods.length} pods running · updated ${fmtTime(dep.updated_at)}`}
        actions={can('operator') && <>
          <div className="btn-group">
            <button disabled={locked} onClick={() => act('start')}><Play size={15} /> Start</button>
            <button disabled={locked} onClick={() => act('stop')}><Square size={15} /> Stop</button>
            <button disabled={locked} onClick={() => act('restart')}><RotateCw size={15} /> Restart</button>
          </div>
          <button className="primary" disabled={locked} onClick={() => setModal('update')}><Upload size={15} /> Update</button>
          <Menu disabled={locked} items={[
            { label: dep.prev_version ? `Roll back to ${dep.prev_version}` : 'Roll back (no previous version)', icon: RotateCcw, disabled: !dep.prev_version,
              onClick: async () => {
                if (!await confirmDialog({ title: `Roll back ${dep.name} to ${dep.prev_version}?`, confirmLabel: 'Roll back' })) return
                run(async () => startJob((await api(`/api/deployments/${id}/rollback`, { method: 'POST' })).job))
              } },
            { label: 'Edit map', icon: Edit3, onClick: () => setModal('map') },
            can('admin') && '-',
            can('admin') && { label: 'Delete deployment', icon: Trash2, danger: true, onClick: () => setModal('delete') },
          ]} />
        </>} />

      {dep.busy_job && <Msg info={<>Job #{dep.busy_job} is running; actions are locked until it finishes. <a href="#"
        onClick={(e) => { e.preventDefault(); setJob(dep.busy_job) }}>View progress</a></>} />}
      {dep.release_changes && (
        <Msg warnings={[<span key="rc">Release {dep.version} was re-uploaded ({fmtTime(dep.release_changes.uploaded_at)}) and differs from what is running here:{' '}
          {[['changed', 'changed'], ['added', 'added'], ['removed', 'removed']].filter(([k]) => dep.release_changes[k].length)
            .map(([k, label]) => `${label} ${dep.release_changes[k].join(', ')}`).join(' · ')}.{' '}
          {can('operator') && <button className="sm primary" disabled={locked} onClick={() => setModal('apply')}>Apply changes</button>}</span>]} />
      )}

      <Card>
        <div className="kv" style={{ marginBottom: 0 }}>
          <div><b>Release</b> {dep.software_name} {dep.version}</div>
          <div><b>Previous version</b> {dep.prev_version || '—'}</div>
          <div><b>Alarm base path</b> <span className="mono small">{dep.alarm_base_path}</span></div>
          <div><b>Nodes</b> {[...new Set(dep.pods.map((p) => p.node).filter(Boolean))].join(', ') || '—'}</div>
        </div>
      </Card>

      <Card flush title="Pods" description="State and health are reported by the agent on each node.">
        <table className="tbl">
          <thead><tr><th>Pod</th><th>Node</th><th>Port</th><th>State</th><th>Health</th><th>Version</th><th>Restarts</th><th>Started</th><th /></tr></thead>
          <tbody>{dep.pods.map((p) => {
            const s = p.status || {}
            return (
              <tr key={p.name}>
                <td className="nowrap"><b>{p.name}</b>{s.crashloop && <> <Badge tone="bad">Crash loop</Badge></>}</td>
                <td className="nowrap">{p.node}<div className="sub mono">{p.node_ip}</div></td>
                <td className="num">{p.port ?? '—'}</td>
                <td className="nowrap"><Badge value={s.state || 'unknown'} />{s.desired === 'stopped' && <div className="sub">Stopped by user</div>}</td>
                <td>{s.health ? <Badge value={s.health} /> : <span className="muted">—</span>}
                  {s.health_output && <div className="sub bad-text" title={s.health_output}>{s.health_output.slice(0, 50)}</div>}</td>
                <td>{s.version || '—'}</td>
                <td className="num">{s.restart_count ?? 0}{s.oom_killed && <span className="bad-text small"> · OOM</span>}</td>
                <td className="small nowrap">{s.started_at ? new Date(s.started_at).toLocaleString() : '—'}</td>
                <td className="actions">
                  {can('operator') && <>
                    <button className="icon" title="Start" aria-label={`Start ${p.name}`} disabled={locked} onClick={() => podAct(p.name, 'start')}><Play size={15} /></button>
                    <button className="icon" title="Stop" aria-label={`Stop ${p.name}`} disabled={locked} onClick={() => podAct(p.name, 'stop')}><Square size={15} /></button>
                    <button className="icon" title="Restart" aria-label={`Restart ${p.name}`} disabled={locked} onClick={() => podAct(p.name, 'restart')}><RotateCw size={15} /></button>
                  </>}
                  <button className="sm" onClick={() => setModal({ logs: p })}><FileText size={14} /> Logs</button>
                  <Menu items={[
                    can('operator') && { label: 'Edit values.env', icon: KeyRound, onClick: () => setModal({ values: p }) },
                    can('operator') && { label: 'Roll back this pod', icon: RotateCcw, disabled: locked || !s.has_prev, onClick: () => podAct(p.name, 'rollback') },
                  ]} />
                </td>
              </tr>
            )
          })}</tbody>
        </table>
      </Card>
      <Backups dep={dep} onJob={startJob} />
      <Jobs depId={id} />
      <Wiring id={id} />
      {modal === 'update' && <UpdateModal dep={dep} onClose={() => setModal(null)} onJob={startJob} />}
      {modal === 'apply' && dep.release_changes && <ApplyChangesModal dep={dep} onClose={() => setModal(null)} onJob={startJob} />}
      {modal === 'delete' && <DeleteModal dep={dep} onClose={() => setModal(null)} onJob={startJob} />}
      {modal === 'map' && (
        <Modal title={`Edit map · ${dep.name}`} onClose={() => setModal(null)} wide>
          <Msg info="Moved pods are installed on their new node and removed from the old one. Pods whose wired addresses change are restarted." />
          <MapEditor version={dep.version} initial={dep.map} depId={dep.id} alarmBase={dep.alarm_base_path} busy={busy}
            submitLabel="Apply map" onCancel={() => setModal(null)}
            onSubmit={(map) => run(async () => startJob((await api(`/api/deployments/${id}/map`, { method: 'PUT', body: { map } })).job))} />
        </Modal>
      )}
      {modal?.logs && <LogsModal pod={modal.logs} onClose={() => setModal(null)} />}
      {modal?.values && <ValuesModal pod={modal.values} onClose={() => setModal(null)} />}
      {job && <JobModal id={job} onClose={() => setJob(null)} />}
    </div>
  )
}

export default function Deployment() {
  const [deps, reload, loadError] = usePoll(() => api('/api/deployments'), 5000)
  const [view, setView] = useState(() => {
    const m = window.location.hash.match(/^#(\d+)$/)
    return m ? Number(m[1]) : null
  })
  const [adding, setAdding] = useState(false)
  const open = (id) => { window.location.hash = id ? String(id) : ''; setView(id); setAdding(false); reload() }
  useEffect(() => {
    const on = () => { const m = window.location.hash.match(/^#(\d+)$/); setView(m ? Number(m[1]) : null) }
    window.addEventListener('hashchange', on)
    return () => window.removeEventListener('hashchange', on)
  }, [])

  if (view) return <DeploymentDetail id={view} onBack={() => open(null)} />
  return (
    <div className="stack">
      <PageHeader title="Deployments"
        description="A deployment places the pods of one release on your nodes. Open one to start, stop, update or roll back."
        actions={can('operator') && !adding && <button className="primary" onClick={() => setAdding(true)}><Plus size={15} /> New deployment</button>} />
      {adding && <AddDeployment onDone={open} onCancel={() => setAdding(false)} />}
      <Card flush title="All deployments">
        <Msg error={loadError} />
        {!deps?.length ? <Empty title={deps ? 'No deployments yet' : 'Loading…'}
          action={deps && can('operator') && !adding && <button className="primary" onClick={() => setAdding(true)}><Plus size={15} /> New deployment</button>}>
          {deps ? 'Create one from a stored release.' : ''}</Empty> : (
          <Pager rows={deps}>{(rows) => (
            <table className="tbl hover">
              <thead><tr><th>Name</th><th>Release</th><th>Nodes</th><th>Pods</th><th>State</th><th>Updated</th></tr></thead>
              <tbody>{rows.map((d) => {
                const healthy = d.pods.filter((p) => p.status?.state === 'running' && p.status?.health !== 'unhealthy').length
                return (
                  <tr key={d.id} onClick={() => open(d.id)} tabIndex={0} onKeyDown={(e) => e.key === 'Enter' && open(d.id)}>
                    <td><b>{d.name}</b></td>
                    <td>{d.software_name}<div className="sub">{d.version}</div></td>
                    <td>{[...new Set(d.pods.map((p) => p.node).filter(Boolean))].join(', ') || '—'}</td>
                    <td><div style={{ width: 160 }}><Progress done={healthy} total={d.pods.length} label={`${healthy}/${d.pods.length} running`}
                      tone={!d.pods.length ? '' : healthy === d.pods.length ? 'ok' : healthy ? 'warn' : 'bad'} /></div></td>
                    <td><Badge value={d.state} />{d.busy_job && <div className="sub">Job #{d.busy_job}</div>}</td>
                    <td>{fmtTime(d.updated_at)}</td>
                  </tr>
                )
              })}</tbody>
            </table>
          )}</Pager>
        )}
      </Card>
    </div>
  )
}
