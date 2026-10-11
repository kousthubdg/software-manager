import { useState } from 'react'
import { Plus, RefreshCw, Scissors, Trash2 } from 'lucide-react'
import { api, can } from '../api.js'
import { Badge, Card, Empty, Field, Menu, Modal, Msg, PageHeader, Progress, ago, confirmDialog, toast, useAction, usePoll } from '../ui.jsx'

export function PodChip({ p }) {
  const bad = p.crashloop || p.health === 'unhealthy' || (p.desired === 'running' && p.state !== 'running')
  const tone = p.crashloop ? 'bad' : bad ? 'warn' : p.state === 'running' ? 'ok' : 'muted'
  return (
    <Badge tone={tone} title={`${p.state}/${p.health || 'no healthcheck'} · desired ${p.desired} · v${p.version}` +
      (p.restart_count ? ` · ${p.restart_count} restarts` : '') + (p.health_output ? `\n${p.health_output}` : '')}>
      {p.name}{p.crashloop ? ' · crash loop' : ''}
    </Badge>
  )
}

const podOk = (p) => p.state === 'running' && !p.crashloop && p.health !== 'unhealthy'

function PodSummary({ pods }) {
  if (!pods.length) return <span className="muted">None</span>
  const ok = pods.filter(podOk).length
  const problems = pods.filter((p) => !podOk(p) && p.desired !== 'stopped')
  return (
    <div style={{ minWidth: 180 }} title={pods.map((p) => `${p.name}: ${p.state}${p.health ? `/${p.health}` : ''}`).join('\n')}>
      <Progress done={ok} total={pods.length} label={`${ok}/${pods.length} running`}
        tone={ok === pods.length ? 'ok' : ok ? 'warn' : 'bad'} />
      {problems.length > 0 && <div className="chips mt" style={{ marginTop: 6 }}>
        {problems.slice(0, 6).map((p) => <PodChip key={p.name} p={p} />)}
        {problems.length > 6 && <span className="muted small">+{problems.length - 6} more</span>}
      </div>}
    </div>
  )
}

function AddNode({ onClose, onAdded }) {
  const [form, setForm] = useState({ name: '', ip: '', port: 2997, token: '' })
  const [error, setError] = useState('')
  const [busy, run] = useAction(setError)
  const set = (k) => (e) => setForm({ ...form, [k]: e.target.value })
  const ready = form.name && form.ip && form.token
  const add = () => run(async () => {
    setError('')
    const r = await api('/api/nodes', { method: 'POST', body: { ...form, port: Number(form.port) } })
    toast(`Added ${form.name}: Docker ${r.info.docker_version}, ${r.info.cpus} CPU, ${r.info.disk_free_gb} GB free`)
    onAdded()
  })
  return (
    <Modal title="Add node" onClose={onClose}
      footer={<><button onClick={onClose}>Cancel</button>
        <button className="primary" disabled={busy || !ready} onClick={add}>{busy ? 'Connecting…' : 'Add node'}</button></>}>
      <p className="dialog-text">Run <code>prereq.sh</code> on the node first. It prints the node token.</p>
      <form className="stack-sm" onSubmit={(e) => { e.preventDefault(); if (ready) add() }}>
        <Field label="Name"><input value={form.name} onChange={set('name')} placeholder="e.g. node-a" /></Field>
        <div className="row gap">
          <div className="grow"><Field label="Node IP"><input value={form.ip} onChange={set('ip')} placeholder="IP address" /></Field></div>
          <div style={{ width: 120 }}><Field label="Agent port"><input type="number" value={form.port} onChange={set('port')} /></Field></div>
        </div>
        <Field label="Node token" hint="Printed by prereq.sh, also in the agent's data/node-token">
          <input type="password" value={form.token} onChange={set('token')} autoComplete="off" /></Field>
        <button type="submit" hidden />
      </form>
      <Msg error={error} />
    </Modal>
  )
}

export default function Nodes() {
  const [nodes, reload, loadError] = usePoll(() => api('/api/nodes'), 5000)
  const [adding, setAdding] = useState(false)
  const [busy, run] = useAction()
  const online = (nodes || []).filter((n) => n.status === 'online').length

  return (
    <div className="stack">
      <PageHeader title="Nodes"
        description="Machines running the sm-agent. Software Manager installs and watches pods on them."
        actions={<>
          <button onClick={reload}><RefreshCw size={15} /> Refresh</button>
          {can('admin') && <button className="primary" onClick={() => setAdding(true)}><Plus size={15} /> Add node</button>}
        </>} />
      <Card flush title="Registered nodes" description={nodes ? `${online} of ${nodes.length} online` : undefined}>
        <Msg error={loadError} />
        {!nodes?.length ? <Empty title={nodes ? 'No nodes yet' : 'Loading…'}
          action={nodes && can('admin') && <button className="primary" onClick={() => setAdding(true)}><Plus size={15} /> Add node</button>}>
          {nodes ? 'Run prereq.sh on each machine, then add it here with the token it prints.' : ''}</Empty> : (
          <table className="tbl">
            <thead><tr><th>Node</th><th>Status</th><th>Docker</th><th>CPU · RAM</th><th>Disk free</th><th>GPU</th>
              <th>Detect capacity</th><th>Pods</th><th>Last seen</th><th /></tr></thead>
            <tbody>
              {nodes.map((n) => {
                const i = n.info || {}
                return (
                  <tr key={n.id}>
                    <td><b>{n.name}</b><div className="sub mono">{n.ip}:{n.port}</div></td>
                    <td><Badge value={n.status} />{n.last_error && <div className="sub bad-text" title={n.last_error}>
                      {n.last_error.slice(0, 60)}</div>}</td>
                    <td>{i.docker_version || '—'}<div className="sub">{i.compose_version}
                      {i.live_restore === false && <span className="warn-text"> · live-restore off</span>}</div></td>
                    <td className="nowrap">{i.cpus ?? '—'} CPU · {i.mem_mb ? `${(i.mem_mb / 1024).toFixed(1)} GB` : '—'}</td>
                    <td className="nowrap">{i.disk_free_gb ?? '—'} / {i.disk_total_gb ?? '—'} GB</td>
                    <td>{i.gpu_nvidia ? 'NVIDIA' : i.dri ? 'iGPU' : <span className="muted">None</span>}</td>
                    <td>{i.bench?.est_detect_fps ? `~${i.bench.est_detect_fps} fps` : <span className="muted">—</span>}</td>
                    <td><PodSummary pods={n.pods || []} /></td>
                    <td className="nowrap">{ago(n.last_seen)}</td>
                    <td className="actions">
                      <button className="icon" title="Refresh node info" aria-label="Refresh node info" disabled={busy}
                        onClick={() => run(async () => { await api(`/api/nodes/${n.id}/refresh`, { method: 'POST' }); reload() })}><RefreshCw size={15} /></button>
                      <Menu items={[
                        can('operator') && { label: 'Prune old images', icon: Scissors, disabled: busy, onClick: async () => {
                          if (!await confirmDialog({ title: `Prune images on ${n.name}?`, message: 'Removes unused older images. Running pods are not affected.', confirmLabel: 'Prune' })) return
                          run(async () => { const r = await api(`/api/nodes/${n.id}/prune`, { method: 'POST' }); toast(`Removed ${r.removed?.length ?? 0} images on ${n.name}`) })
                        } },
                        can('admin') && { label: 'Remove node', icon: Trash2, danger: true, onClick: async () => {
                          if (!await confirmDialog({ title: `Remove node ${n.name}?`, message: 'The node is removed from Software Manager. Containers on it keep running.', confirmLabel: 'Remove node', danger: true })) return
                          run(async () => { await api(`/api/nodes/${n.id}`, { method: 'DELETE' }); toast(`Removed ${n.name}`); reload() })
                        } },
                      ]} />
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        )}
      </Card>
      {adding && <AddNode onClose={() => setAdding(false)} onAdded={() => { setAdding(false); reload() }} />}
    </div>
  )
}
