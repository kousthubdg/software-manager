import { useEffect, useState } from 'react'
import { KeyRound, Plus, Save, Trash2 } from 'lucide-react'
import { api, can, qs } from '../api.js'
import {
  Badge, Card, CopyButton, Empty, Field, Menu, Msg, PageHeader, confirmDialog, fmtTime, promptDialog, toast, useAction, usePoll,
} from '../ui.jsx'
import { JobModal } from './Deployment.jsx'

function Section({ title, children, about }) {
  return (
    <div className="form-section">
      <div className="about"><h3>{title}</h3>{about && <p>{about}</p>}</div>
      <div className="form-grid">{children}</div>
    </div>
  )
}

const GENERAL_KEYS = ['data_root', 'run_uid', 'run_gid', 'logger_url', 'logger_url_for_agents', 'camera_count', 'detect_fps_per_camera',
  'trivy_block_critical', 'manager_node_id', 'mqtt_alert_user', 'fileserver_base_url', 'fileserver_user']
const norm = (x) => (x === null || x === undefined ? '' : String(x))

function General() {
  const [s, reload, loadError] = usePoll(() => api('/api/settings'), 0)
  const [nodes] = usePoll(() => api('/api/nodes'), 0)
  const [v, setV] = useState({})
  const [secrets, setSecrets] = useState({})
  const [error, setError] = useState('')
  const [busy, run] = useAction(setError)
  useEffect(() => { if (s) setV(s) }, [s])
  if (!s) return <Card>{loadError ? <Msg error={loadError} /> : <Empty>Loading…</Empty>}</Card>
  const set = (k) => (e) => setV({ ...v, [k]: e.target.type === 'checkbox' ? e.target.checked : e.target.value })
  const setSecret = (k) => (e) => setSecrets({ ...secrets, [k]: e.target.value })
  const dirty = GENERAL_KEYS.some((k) => norm(v[k]) !== norm(s[k])) || Object.values(secrets).some(Boolean)
  const save = () => run(async () => {
    setError('')
    const values = Object.fromEntries(GENERAL_KEYS.map((k) => [k, v[k] ?? null]))
    await api('/api/settings', { method: 'PUT', body: { values, ...secrets } })
    toast('Settings saved')
    setSecrets({})
    reload()
  })
  const discard = () => { setV(s); setSecrets({}); setError('') }
  const ro = !can('admin')
  const setTag = (on) => (on ? 'set' : 'not set')
  return (
    <>
      <Card title="Site" description={`${s.branch ? `Branch ${s.branch} · ` : ''}HTTPS ${s.https ? 'on' : 'off'}${ro ? ' · read only (admin role needed to change)' : ''}`}>
        <Section title="Pods and storage" about="Where pod data lives on every node and which user owns it.">
          <Field label="Data root" tag="SM_DATA_ROOT" hint="Pods live in <data root>/<pod>/"><input disabled={ro} value={v.data_root || ''} onChange={set('data_root')} /></Field>
          <Field label="Run UID"><input disabled={ro} value={v.run_uid || ''} onChange={set('run_uid')} inputMode="numeric" /></Field>
          <Field label="Run GID"><input disabled={ro} value={v.run_gid || ''} onChange={set('run_gid')} inputMode="numeric" /></Field>
          <Field label="Software Manager node" hint="Used to self-update the manager and logger">
            <select disabled={ro} value={v.manager_node_id || ''} onChange={set('manager_node_id')}>
              <option value="">Automatic (SYS_IP)</option>{nodes?.map((n) => <option key={n.id} value={n.id}>{n.name} ({n.ip})</option>)}
            </select></Field>
        </Section>
        <Section title="Capacity" about="Used to warn when a deployment map places too many cameras on a node.">
          <Field label="Cameras at this branch"><input disabled={ro} type="number" value={v.camera_count || ''} onChange={set('camera_count')} /></Field>
          <Field label="Detect fps per camera"><input disabled={ro} type="number" value={v.detect_fps_per_camera || ''} onChange={set('detect_fps_per_camera')} placeholder="5" /></Field>
        </Section>
        <Section title="Logging" about="Where Software Manager and the agents send logs.">
          <Field label="Logger URL" hint="Manager → logger"><input disabled={ro} value={v.logger_url || ''} onChange={set('logger_url')} /></Field>
          <Field label="Logger URL for agents" hint="Default: the logger URL with this node's IP"><input disabled={ro} value={v.logger_url_for_agents || ''} onChange={set('logger_url_for_agents')} /></Field>
          <Field label="Logger token" tag={setTag(s.logger_token_set)}><input disabled={ro} type="password" placeholder="Unchanged" value={secrets.logger_token || ''} onChange={setSecret('logger_token')} autoComplete="new-password" /></Field>
        </Section>
        <Section title="Alerts" about="MQTT login used to publish crash-loop and health alerts.">
          <Field label="MQTT user"><input disabled={ro} value={v.mqtt_alert_user || ''} onChange={set('mqtt_alert_user')} /></Field>
          <Field label="MQTT password" tag={setTag(s.mqtt_alert_password_set)}><input disabled={ro} type="password" placeholder="Unchanged" value={secrets.mqtt_alert_password || ''} onChange={setSecret('mqtt_alert_password')} autoComplete="new-password" /></Field>
        </Section>
        <Section title="File server" about="Optional http(s) base for scripts and datafiles referenced in a release YML.">
          <Field label="Base URL"><input disabled={ro} value={v.fileserver_base_url || ''} onChange={set('fileserver_base_url')} placeholder="https://…" /></Field>
          <Field label="User"><input disabled={ro} value={v.fileserver_user || ''} onChange={set('fileserver_user')} /></Field>
          <Field label="Password" tag={setTag(s.fileserver_password_set)}><input disabled={ro} type="password" placeholder="Unchanged" value={secrets.fileserver_password || ''} onChange={setSecret('fileserver_password')} autoComplete="new-password" /></Field>
        </Section>
        <Section title="Security" about="Image vulnerability scan with trivy when a release is checked.">
          <label className="check" style={{ gridColumn: '1 / -1' }}><input disabled={ro} type="checkbox" checked={!!v.trivy_block_critical} onChange={set('trivy_block_critical')} />
            Block a release that has CRITICAL vulnerabilities</label>
        </Section>
        <Msg error={error} />
      </Card>
      {dirty && !ro && (
        <div className="savebar" role="region" aria-label="Unsaved changes">
          <span>You have unsaved changes</span>
          <div className="row gap">
            <button className="ghost" onClick={discard} disabled={busy}>Discard</button>
            <button className="primary" onClick={save} disabled={busy}><Save size={15} /> {busy ? 'Saving…' : 'Save changes'}</button>
          </div>
        </div>
      )}
    </>
  )
}

function Catalog() {
  const [st, reload] = usePoll(() => api('/api/central'), 0)
  const [f, setF] = useState({ url: '', key: '', ca_pem: '' })
  const [newKey, setNewKey] = useState('')
  const [error, setError] = useState('')
  const [busy, run] = useAction(setError)
  useEffect(() => { if (st) setF((x) => ({ ...x, url: st.url || '' })) }, [st])
  if (!st) return <Card><Empty>Loading…</Empty></Card>
  const ro = !can('admin')
  const dirty = f.url !== (st.url || '') || f.key || f.ca_pem
  const save = () => run(async () => {
    setError('')
    await api('/api/central', { method: 'PUT', body: { url: f.url, key: f.key || null, ca_pem: f.ca_pem || null } })
    setF({ ...f, key: '', ca_pem: '' })
    await api('/api/central/check', { method: 'POST' })
    toast('Saved. Checking the office now.')
    reload()
  })
  const state = !st.url ? <Badge value="not connected" tone="muted" /> : st.error ? <Badge value="error" /> : <Badge value="connected" tone="ok" />
  return (
    <>
      <Card title={<>Get releases from the office {state}</>}
        description="For a branch: new releases uploaded at the office download here by themselves. Installing stays manual."
        footer={!ro && <>
          {dirty && <span className="muted small grow">Unsaved changes</span>}
          <button className="primary" disabled={busy || !dirty} onClick={save}><Save size={15} /> Save and check</button>
        </>}>
        <p className="lead">This branch asks the office every {Math.round(st.poll_sec / 60)} min over outbound HTTPS, so it works behind NAT
          and through a VPN. Last check: {fmtTime(st.last_check)}.</p>
        <div className="form-grid">
          <Field label="Office address" hint="https://<office host or IP>:<port>">
            <input disabled={ro} value={f.url} onChange={(e) => setF({ ...f, url: e.target.value })} placeholder="https://office-host:2999" /></Field>
          <Field label="Branch key" tag={st.key_set ? 'set' : 'not set'} hint="Generated at the office under Settings → Release catalog">
            <input disabled={ro} type="password" placeholder={st.key_set ? 'Unchanged' : 'Paste the key'} value={f.key}
              onChange={(e) => setF({ ...f, key: e.target.value })} autoComplete="new-password" /></Field>
        </div>
        <div className="mt">
          <Field label="Office CA certificate" tag={st.ca_set ? 'set' : 'optional'}
            hint={st.ca_set ? 'Leave empty to keep the stored certificate.' : 'Without it the connection is encrypted but the office certificate is not verified.'}>
            <textarea disabled={ro} rows={3} value={f.ca_pem} onChange={(e) => setF({ ...f, ca_pem: e.target.value })} placeholder="-----BEGIN CERTIFICATE-----" /></Field>
        </div>
        <Msg error={error || st.error} />
      </Card>
      {!ro && (
        <Card title={<>Publish releases to branches {st.catalog_enabled ? <Badge value="on" tone="ok">On</Badge> : <Badge value="off" tone="muted">Off</Badge>}</>}
          description="For the office: branches with this address and the branch key receive every stored release.">
          <p className="lead">Generating a new key replaces the old one, and every branch then needs the new key.</p>
          <div className="row gap wrap">
            <button className={st.catalog_enabled ? '' : 'primary'} disabled={busy} onClick={async () => {
              if (st.catalog_enabled && !await confirmDialog({ title: 'Replace the branch key?', message: 'Branches using the current key stop receiving releases until they get the new one.', confirmLabel: 'Generate new key', danger: true })) return
              run(async () => { setError(''); setNewKey((await api('/api/catalog/key', { method: 'POST' })).key); reload() })
            }}><KeyRound size={15} /> {st.catalog_enabled ? 'Generate new branch key' : 'Turn on publishing'}</button>
            {st.catalog_enabled && <button className="danger" disabled={busy} onClick={async () => {
              if (!await confirmDialog({ title: 'Turn off publishing?', message: 'Branches stop receiving new releases from this Software Manager.', confirmLabel: 'Turn off', danger: true })) return
              run(async () => { await api('/api/catalog/key', { method: 'DELETE' }); setNewKey(''); reload() })
            }}>Turn off publishing</button>}
          </div>
          {newKey && (
            <div className="mt">
              <Field label="Branch key" hint="Shown only once. Enter it in each branch under Settings → Release catalog.">
                <div className="secret-box"><code>{newKey}</code><CopyButton text={newKey} /></div>
              </Field>
            </div>
          )}
        </Card>
      )}
    </>
  )
}

function Registries() {
  const [list, reload] = usePoll(() => api('/api/registries'), 0)
  const empty = { host: '', username: '', password: '', ca_pem: '', insecure: false }
  const [f, setF] = useState(empty)
  const [test, setTest] = useState({ repo: '', tag: '' })
  const [error, setError] = useState('')
  const [busy, run] = useAction(setError)
  const save = () => run(async () => {
    setError('')
    await api('/api/registries', { method: 'PUT', body: { ...f, password: f.password || null } })
    toast(`Saved registry ${f.host}`)
    setF(empty)
    reload()
  })
  const editing = list?.some((r) => r.host === f.host)
  return (
    <>
      <Card flush title="Registries" description="Image registries Software Manager reads when a release is checked and stored."
        actions={can('operator') && <>
          <input aria-label="Test repository" value={test.repo} onChange={(e) => setTest({ ...test, repo: e.target.value })} placeholder="Test repository" style={{ width: 160 }} />
          <input aria-label="Test tag" value={test.tag} onChange={(e) => setTest({ ...test, tag: e.target.value })} placeholder="Tag" style={{ width: 90 }} />
        </>}>
        {!list?.length ? <Empty>{list ? 'No registry credentials yet.' : 'Loading…'}</Empty> : (
          <table className="tbl">
            <thead><tr><th>Host</th><th>User</th><th>Password</th><th>CA</th><th>TLS</th><th /></tr></thead>
            <tbody>{list.map((r) => (
              <tr key={r.host}><td><span className="mono small">{r.host}</span></td><td>{r.username || '—'}</td>
                <td>{r.password_set ? 'Set' : <span className="muted">—</span>}</td>
                <td>{r.ca ? 'Custom CA' : 'System'}</td>
                <td>{r.insecure ? <Badge tone="warn">Not verified</Badge> : <Badge tone="ok">Verified</Badge>}</td>
                <td className="actions">
                  {can('operator') && <button className="sm" disabled={busy || !test.repo || !test.tag}
                    title={!test.repo || !test.tag ? 'Enter a repository and tag above to test' : ''} onClick={() => run(async () => {
                      setError('')
                      const t = await api(`/api/registries/${encodeURIComponent(r.host)}/test?${qs(test)}`, { method: 'POST' })
                      t.ok ? toast(`${r.host}/${test.repo}:${test.tag} → ${t.digest}`) : setError(t.error)
                    })}>Test</button>}
                  {can('admin') && <Menu items={[
                    { label: 'Edit', onClick: () => setF({ ...empty, host: r.host, username: r.username, insecure: r.insecure }) },
                    { label: 'Remove', icon: Trash2, danger: true, onClick: async () => {
                      if (!await confirmDialog({ title: `Remove registry ${r.host}?`, confirmLabel: 'Remove', danger: true })) return
                      run(async () => { await api(`/api/registries/${encodeURIComponent(r.host)}`, { method: 'DELETE' }); reload() })
                    } },
                  ]} />}
                </td></tr>
            ))}</tbody>
          </table>
        )}
        <Msg error={error} />
      </Card>
      {can('admin') && (
        <Card title={editing ? `Edit ${f.host}` : 'Add registry'}
          footer={<>
            {f.host && <button onClick={() => setF(empty)}>Cancel</button>}
            <button className="primary" disabled={busy || !f.host} onClick={save}><Save size={15} /> Save registry</button>
          </>}>
          <div className="form-grid">
            <Field label="Host:port"><input value={f.host} onChange={(e) => setF({ ...f, host: e.target.value })} placeholder="registry-host:5000" /></Field>
            <Field label="User"><input value={f.username} onChange={(e) => setF({ ...f, username: e.target.value })} autoComplete="off" /></Field>
            <Field label="Password"><input type="password" value={f.password} placeholder={editing ? 'Unchanged' : ''} onChange={(e) => setF({ ...f, password: e.target.value })} autoComplete="new-password" /></Field>
          </div>
          <div className="mt">
            <Field label="CA certificate (PEM)" hint="The registry's ca.crt (docker-registry: data/certs/ca.crt)">
              <textarea rows={4} value={f.ca_pem} onChange={(e) => setF({ ...f, ca_pem: e.target.value })} placeholder="-----BEGIN CERTIFICATE-----" /></Field>
          </div>
          <label className="check mt"><input type="checkbox" checked={f.insecure} onChange={(e) => setF({ ...f, insecure: e.target.checked })} />
            Skip TLS verification (insecure, lab use only)</label>
        </Card>
      )}
    </>
  )
}

function Users() {
  const [list, reload] = usePoll(() => api('/api/users'), 0)
  const [f, setF] = useState({ username: '', password: '', role: 'viewer' })
  const [error, setError] = useState('')
  const [busy, run] = useAction(setError)
  const put = (u, patch) => run(async () => {
    await api(`/api/users/${encodeURIComponent(u.username)}`, { method: 'PUT', body: { role: u.role, disabled: !!u.disabled, ...patch } })
    reload()
  })
  return (
    <>
      <Card flush title="Users and roles"
        description="Viewer: read only · Operator: upload, deploy, start/stop/update, download logs · Admin: users, settings, delete, secrets">
        <Msg error={error} />
        <table className="tbl">
          <thead><tr><th>User</th><th>Role</th><th>Status</th><th>Created</th><th /></tr></thead>
          <tbody>{list?.map((u) => (
            <tr key={u.username}>
              <td><div className="row gap center"><span className="avatar">{u.username.slice(0, 2)}</span><b>{u.username}</b></div></td>
              <td><select aria-label={`Role of ${u.username}`} value={u.role} onChange={(e) => put(u, { role: e.target.value })}>
                {['viewer', 'operator', 'admin'].map((r) => <option key={r} value={r}>{r[0].toUpperCase() + r.slice(1)}</option>)}</select></td>
              <td><label className="check"><input type="checkbox" checked={!u.disabled} onChange={(e) => put(u, { disabled: !e.target.checked })} />
                {u.disabled ? 'Disabled' : 'Enabled'}</label></td>
              <td>{fmtTime(u.created_at)}</td>
              <td className="actions">
                <Menu items={[
                  { label: 'Reset password', icon: KeyRound, onClick: async () => {
                    const p = await promptDialog({ title: `Reset password of ${u.username}`, label: 'New password', type: 'password', minLength: 8, confirmLabel: 'Set password' })
                    if (p) run(async () => { await api(`/api/users/${encodeURIComponent(u.username)}`, { method: 'PUT', body: { role: u.role, disabled: !!u.disabled, password: p } }); toast(`Password of ${u.username} changed`) })
                  } },
                  { label: 'Delete user', icon: Trash2, danger: true, onClick: async () => {
                    if (!await confirmDialog({ title: `Delete ${u.username}?`, confirmLabel: 'Delete user', danger: true })) return
                    run(async () => { await api(`/api/users/${encodeURIComponent(u.username)}`, { method: 'DELETE' }); reload() })
                  } },
                ]} />
              </td></tr>
          ))}</tbody>
        </table>
      </Card>
      <Card title="Add user" footer={<button className="primary" disabled={busy || !f.username || f.password.length < 8} onClick={() => run(async () => {
        await api('/api/users', { method: 'POST', body: f }); toast(`Added ${f.username}`); setF({ username: '', password: '', role: 'viewer' }); reload()
      })}><Plus size={15} /> Add user</button>}>
        <div className="form-grid">
          <Field label="Username"><input value={f.username} onChange={(e) => setF({ ...f, username: e.target.value })} autoComplete="off" /></Field>
          <Field label="Password" hint="At least 8 characters"><input type="password" value={f.password} onChange={(e) => setF({ ...f, password: e.target.value })} autoComplete="new-password" /></Field>
          <Field label="Role"><select value={f.role} onChange={(e) => setF({ ...f, role: e.target.value })}>
            {['viewer', 'operator', 'admin'].map((r) => <option key={r} value={r}>{r[0].toUpperCase() + r.slice(1)}</option>)}</select></Field>
        </div>
      </Card>
    </>
  )
}

function Account() {
  const [f, setF] = useState({ old_password: '', new_password: '' })
  const [error, setError] = useState('')
  const [busy, run] = useAction(setError)
  return (
    <Card title="Change password" footer={<button className="primary" disabled={busy || !f.old_password || f.new_password.length < 8} onClick={() => run(async () => {
      setError('')
      await api('/api/auth/password', { method: 'POST', body: f }); toast('Password changed'); setF({ old_password: '', new_password: '' })
    })}>Change password</button>}>
      <div className="form-grid">
        <Field label="Current password"><input type="password" value={f.old_password} onChange={(e) => setF({ ...f, old_password: e.target.value })} autoComplete="current-password" /></Field>
        <Field label="New password" hint="At least 8 characters"><input type="password" value={f.new_password} onChange={(e) => setF({ ...f, new_password: e.target.value })} autoComplete="new-password" /></Field>
      </div>
      <Msg error={error} />
    </Card>
  )
}

function SelfUpdate() {
  const [f, setF] = useState({ component: 'sm-agent', image: '' })
  const [job, setJob] = useState(null)
  const [error, setError] = useState('')
  const [busy, run] = useAction(setError)
  return (
    <Card title="Update the Software Manager stack"
      description="Replaces a component's image and keeps its settings and data. Rolls back automatically if the new one is not healthy."
      footer={<button className="primary" disabled={busy || !f.image} onClick={async () => {
        if (!await confirmDialog({ title: `Update ${f.component}?`, message: `New image: ${f.image}`, confirmLabel: 'Update' })) return
        run(async () => setJob((await api('/api/self-update', { method: 'POST', body: f })).job))
      }}>Update</button>}>
      <div className="form-grid">
        <Field label="Component" hint="Agents update on every node; logger and manager on the manager node">
          <select value={f.component} onChange={(e) => setF({ ...f, component: e.target.value })}>
            {['sm-agent', 'sm-logger', 'software-manager'].map((c) => <option key={c}>{c}</option>)}</select></Field>
        <div style={{ gridColumn: 'span 2' }}>
          <Field label="Image" hint="registry/repository:tag"><input value={f.image} onChange={(e) => setF({ ...f, image: e.target.value })}
            placeholder={`registry-host:5000/${f.component}:<version>`} /></Field>
        </div>
      </div>
      <Msg error={error} />
      {job && <JobModal id={job} onClose={() => setJob(null)} />}
    </Card>
  )
}

function Audit() {
  const [q, setQ] = useState('')
  const [rows] = usePoll(() => api(`/api/audit?limit=300&q=${encodeURIComponent(q)}`), 0, [q])
  return (
    <Card flush title="Audit log" description="Every change made through Software Manager (last 300 entries)."
      actions={<input aria-label="Filter audit log" placeholder="Filter…" value={q} onChange={(e) => setQ(e.target.value)} style={{ width: 220 }} />}>
      <div className="scroll">
        {!rows?.length ? <Empty>{rows ? 'No entries.' : 'Loading…'}</Empty> : (
          <table className="tbl compact">
            <thead><tr><th>Time</th><th>User</th><th>Action</th><th>Target</th><th>Detail</th><th>IP</th></tr></thead>
            <tbody>{rows.map((a) => (
              <tr key={a.id}><td className="nowrap">{fmtTime(a.ts)}</td><td>{a.username}</td><td><span className="mono small">{a.action}</span></td>
                <td>{a.target}</td><td className="wrap-text small muted">{a.detail}</td><td className="mono small">{a.ip}</td></tr>
            ))}</tbody>
          </table>
        )}
      </div>
    </Card>
  )
}

const TABS = [
  ['general', 'General', General, 'viewer'], ['catalog', 'Release catalog', Catalog, 'viewer'],
  ['registries', 'Registries', Registries, 'viewer'], ['users', 'Users', Users, 'admin'],
  ['account', 'My account', Account, 'viewer'], ['update', 'Self-update', SelfUpdate, 'admin'], ['audit', 'Audit log', Audit, 'admin'],
]

export default function Settings() {
  const tabs = TABS.filter((t) => can(t[3]))
  const fromHash = () => { const h = window.location.hash.slice(1); return tabs.some((t) => t[0] === h) ? h : tabs[0][0] }
  const [tab, setTab] = useState(fromHash)
  useEffect(() => {
    const on = () => setTab(fromHash())
    window.addEventListener('popstate', on)
    window.addEventListener('hashchange', on)
    return () => { window.removeEventListener('popstate', on); window.removeEventListener('hashchange', on) }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])
  const choose = (id) => { window.history.replaceState({}, '', `#${id}`); setTab(id) }
  const T = tabs.find((t) => t[0] === tab)[2]
  return (
    <div>
      <PageHeader title="Settings" description="Site configuration, release sources, access and maintenance." />
      <div className="settings">
        <nav className="subnav" aria-label="Settings sections">
          {tabs.map(([id, label]) => (
            <button key={id} className={tab === id ? 'active' : ''} aria-current={tab === id ? 'page' : undefined} onClick={() => choose(id)}>{label}</button>
          ))}
        </nav>
        <div className="stack"><T /></div>
      </div>
    </div>
  )
}
