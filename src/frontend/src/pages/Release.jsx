import { useState } from 'react'
import { Check, Download, FolderOpen, Globe, Package, RefreshCw, Trash2, Upload, X } from 'lucide-react'
import { api, can, download } from '../api.js'
import {
  Badge, Card, Empty, Menu, Msg, PageHeader, Pager, Progress, Tabs, confirmDialog, fmtBytes, fmtTime, navigate, toast,
  useAction, usePoll,
} from '../ui.jsx'

function ScanCell({ check }) {
  if (!check) return <span className="muted">—</span>
  if (!check.ok) return <span className="bad-text row gap center" title={check.error}><X size={14} /> {check.error}</span>
  return (
    <span className="row gap center nowrap"><Check size={14} className="ok-text" />
      <span className="mono small">{check.digest ? check.digest.slice(7, 19) : check.md5 ? `md5 ${check.md5.slice(0, 8)}` : 'ok'}</span>
      {check.size ? <span className="muted small">{fmtBytes(check.size)}</span> : null}
    </span>
  )
}

function ReleaseDetail({ version, onDeleted, onClose }) {
  const busyStates = ['scanning', 'downloading', 'uploaded']
  const v = encodeURIComponent(version)
  const [rel, reload, loadError] = usePoll(() => api(`/api/releases/${v}`), 2500, [version])
  const [error, setError] = useState('')
  const [busy, run] = useAction(setError)
  if (!rel) return <Card title={`Release ${version}`}>{loadError ? <Msg error={loadError} /> : <Empty>Loading…</Empty>}</Card>
  const scan = rel.scan || {}
  const byName = Object.fromEntries((scan.installers || []).map((x) => [x.name, x]))
  const items = rel.progress?.items || {}
  const act = (path) => run(async () => { await api(path, { method: 'POST' }); setError(''); reload() })
  const inUse = rel.used_by?.length > 0

  return (
    <Card title={<>Release {rel.version} <Badge value={rel.status} /></>}
      description={`${rel.software_name}${rel.description ? ` · ${rel.description}` : ''}`}
      actions={<>
        {can('operator') && <>
          <button disabled={busy || busyStates.includes(rel.status) || rel.status === 'downloaded'}
            onClick={() => act(`/api/releases/${v}/scan`)}><RefreshCw size={15} /> Check again</button>
          <button className="primary" disabled={busy || !scan.ok || !['scanned', 'download_failed'].includes(rel.status)}
            title="Store the scripts and image of every service at this Software Manager"
            onClick={() => act(`/api/releases/${v}/download`)}><Download size={15} /> Store</button>
        </>}
        {can('admin') && <Menu items={[{
          label: inUse ? `Delete (used by ${rel.used_by.join(', ')})` : 'Delete release', icon: Trash2, danger: true, disabled: busy || inUse,
          onClick: async () => {
            if (!await confirmDialog({ title: `Delete release ${version}?`, message: 'The stored scripts, images and datafiles of this release are removed from this Software Manager. The release path is not touched.', confirmLabel: 'Delete release', danger: true })) return
            run(async () => { await api(`/api/releases/${v}`, { method: 'DELETE' }); toast(`Release ${version} deleted`); onDeleted() })
          },
        }]} />}
        <button className="icon" title="Close details" aria-label="Close details" onClick={onClose}><X size={16} /></button>
      </>}>
      <div className="kv">
        <div><b>Uploaded</b> {fmtTime(rel.uploaded_at)} by {rel.uploaded_by}{rel.revision > 1 ? ` · revision ${rel.revision}` : ''}</div>
        <div><b>Manifest sha256</b> <span className="mono small">{rel.manifest_sha256?.slice(0, 16)}…</span></div>
        <div><b>Used by</b> {inUse ? rel.used_by.join(', ') : 'No deployment'}</div>
        {rel.progress?.phase && <div><b>Progress</b>
          <Progress done={rel.progress.done ?? 0} total={rel.progress.total ?? 0}
            label={`${rel.progress.phase} ${rel.progress.done ?? 0}/${rel.progress.total ?? 0}${rel.progress.current ? ` · ${rel.progress.current}` : ''}`} /></div>}
      </div>
      {rel.reuploading && <Msg info={`Re-uploading ${rel.version}: deployments keep using the stored copy until the new one is checked and stored.`} />}
      {rel.progress?.reupload_error && <Msg error={`Re-upload failed ${fmtTime(rel.progress.reupload_failed_at)}; the previous copy was kept. ${rel.progress.reupload_error}`} />}
      <Msg error={error || rel.progress?.error} warnings={[...(scan.warnings || []),
        ...(scan.installers || []).flatMap((x) => (x.warnings || []).map((w) => `${x.name}: ${w}`))]} />
      <div className="mt" style={{ overflowX: 'auto' }}>
        <table className="tbl compact">
          <thead><tr><th>Service</th><th>Image</th><th>Scripts</th><th>Datafile</th><th>Image check</th>
            <th>Scripts check</th><th>Datafile check</th><th>Stored</th></tr></thead>
          <tbody>
            {rel.manifest?.installers.map((i) => (
              <tr key={i.name}>
                <td className="nowrap"><b>{i.name}</b>{i.self_update && <span className="chip">self</span>}</td>
                <td className="nowrap"><span className="mono small">{i.image}:{i.image_ver}</span>{i.image_source === 'tar' && <span className="chip">tar</span>}</td>
                <td>{i.scripts_ver}</td>
                <td>{i.datafile_path ? i.datafile_ver : <span className="muted">—</span>}</td>
                <td><ScanCell check={byName[i.name]?.checks?.image} /></td>
                <td><ScanCell check={byName[i.name]?.checks?.scripts} /></td>
                <td>{i.datafile_path ? <ScanCell check={byName[i.name]?.checks?.datafile} /> : <span className="muted">—</span>}</td>
                <td>{rel.status === 'downloaded' ? <Badge value="downloaded" /> : items[i.name] ? <Badge value={items[i.name]} /> : <span className="muted">—</span>}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <details className="mt">
        <summary>Manifest YAML</summary>
        <pre className="pre">{rel.manifest_text}</pre>
      </details>
    </Card>
  )
}

function confirmReupload({ version, uploaded_at, uploaded_by }) {
  return confirmDialog({
    title: `Re-upload release ${version}?`,
    message: `You are re-uploading ${version} (stored ${fmtTime(uploaded_at)}${uploaded_by ? ` by ${uploaded_by}` : ''}). `
      + 'Every image and scripts tar is checked and stored again, so a re-pushed image or an added service is picked up. '
      + 'The stored copy stays in use until the new one is ready; if anything fails it is kept. '
      + `Deployments on ${version} then offer "Apply changes".`,
    confirmLabel: `Re-upload ${version}`,
  })
}

function ReleasePath({ onPick, reloadList }) {
  const [src, reload, loadError] = usePoll(() => api('/api/release-source'), 5000)
  const [folder, setFolder] = useState('')
  const [error, setError] = useState('')
  const [ok, setOk] = useState('')
  const [busy, run] = useAction(setError)
  const items = src?.releases || []
  const cur = items.find((x) => x.folder === folder)
  const stored = cur?.status === 'downloaded'

  const doUpload = async () => {
    if (stored && !await confirmReupload(cur)) return
    run(async () => {
      setError('')
      setOk('')
      const path = `/api/release-source/${encodeURIComponent(folder)}/import`
      let res
      try {
        res = await api(path, { method: 'POST', body: { download: true, replace: stored } })
      } catch (e) {
        if (!e.data?.exists || !await confirmReupload(e.data)) throw e
        res = await api(path, { method: 'POST', body: { download: true, replace: true } })
      }
      setOk(res.reupload ? `Re-uploading ${res.version}: checking, then storing every service again. The previous copy stays in use until this finishes.`
        : res.status === 'downloaded' ? `Release ${res.version} is already stored at this Software Manager.`
          : `Release ${res.version}: checking, then storing every service (scripts + image). Progress is in the table below.`)
      onPick(res.version)
      reload()
      reloadList()
    })
  }

  if (src && !src.exists) {
    return <Empty title="Release path not mounted">The release path <code>{src.root}</code> is not available on this host (SM_RELEASE_SOURCE).</Empty>
  }
  return (
    <div>
      <p className="lead">Pick a version folder from the release path <code>{src?.root || '…'}</code> (read only). Software Manager reads its{' '}
        <code>upload_releaseinfo_*.yml</code>, checks every image and scripts tar, then stores a copy of each service here.</p>
      <div className="row gap end wrap">
        <label className="field grow" style={{ maxWidth: 560 }}>
          <span className="field-label">Version folder</span>
          <select value={folder} onChange={(e) => { setFolder(e.target.value); setOk(''); setError('') }}>
            <option value="">{src ? (items.length ? 'Select a folder…' : 'No version folders found') : 'Loading…'}</option>
            {items.map((x) => (
              <option key={x.folder} value={x.folder}>
                {x.folder}{x.software_name ? ` — ${x.software_name} ${x.version}` : ''}{x.installers ? ` · ${x.installers} services` : ''}
                {x.status ? ` · ${x.status}` : ''}
              </option>
            ))}
          </select>
        </label>
        {can('operator') && (
          <button className="primary" disabled={busy || !cur?.version || cur?.reuploading || ['downloading', 'scanning', 'uploaded'].includes(cur?.status)}
            title={stored ? `Check and store ${cur.version} again from this folder` : undefined}
            onClick={doUpload}>{stored ? <><RefreshCw size={15} /> Re-upload {cur.version}</> : <><Upload size={15} /> Upload</>}</button>
        )}
      </div>
      {cur && (
        <div className="kv mt">
          <div><b>Manifest</b> {cur.manifest || '—'}</div>
          {cur.description && <div><b>Description</b> {cur.description}</div>}
          <div><b>Here</b> {cur.status ? <a href="#" onClick={(e) => { e.preventDefault(); onPick(cur.version) }}><Badge value={cur.reuploading ? 're-uploading' : cur.status} /></a>
            : <span className="muted">Not uploaded yet</span>}
            {stored && <span className="muted small"> · stored {fmtTime(cur.uploaded_at)}{cur.uploaded_by ? ` by ${cur.uploaded_by}` : ''}{cur.revision > 1 ? ` · revision ${cur.revision}` : ''}</span>}</div>
        </div>
      )}
      {stored && !cur.reuploading && <Msg warnings={[`${cur.version} is already stored here. Upload becomes Re-upload: use it after changing ${cur.manifest || 'the YML'}, re-pushing an image with the same tag or adding a service.${cur.manifest_changed ? ' The YML in this folder differs from the stored one.' : ''}`]} />}
      <Msg error={error || cur?.error || loadError} ok={ok} />
    </div>
  )
}

function UploadFile({ onPick, reloadList }) {
  const [text, setText] = useState('')
  const [file, setFile] = useState(null)
  const [error, setError] = useState('')
  const [result, setResult] = useState(null)
  const [busy, run] = useAction(setError)
  const send = (replace) => {
    if (file) {
      const fd = new FormData()
      fd.append('file', file)
      return api(`/api/releases/upload${replace ? '?replace=true' : ''}`, { method: 'POST', form: fd })
    }
    return api('/api/releases/upload', { method: 'POST', body: { text, replace } })
  }
  const upload = () => run(async () => {
    setError('')
    setResult(null)
    let res
    try {
      try {
        res = await send(false)
      } catch (e) {
        if (!e.data?.exists) throw e
        if (!await confirmReupload(e.data)) return
        res = await send(true)
      }
    } catch (e) {
      setError(e.data?.errors ? e.data.errors.join('\n') : e.message)
      if (e.data?.warnings) setResult({ warnings: e.data.warnings })
      return
    }
    setResult(res)
    onPick(res.version)
    setFile(null)
    setText('')
    reloadList()
  })
  if (!can('operator')) return <Empty>Uploading a release file needs the operator role.</Empty>
  return (
    <div className="stack-sm">
      <p className="lead">For a release YML that is not in the release path (format <code>upload_releaseinfo</code>: software_name,
        software_version, installers with image, image_ver, scripts_path, datafile_path). Paths must be absolute or https URLs.</p>
      <div className="grid2">
        <label className="field">
          <span className="field-label">YML file</span>
          <input type="file" accept=".yml,.yaml,.txt" onChange={(e) => setFile(e.target.files[0] || null)} />
        </label>
        <label className="field">
          <span className="field-label">…or paste the manifest</span>
          <textarea rows={5} value={text} onChange={(e) => setText(e.target.value)} placeholder={'software_name: YELLOW_METAL_PLATFORM\nsoftware_version: 1.0.1\ninstallers: …'} />
        </label>
      </div>
      <div><button className="primary" disabled={busy || (!file && !text.trim())} onClick={upload}><Upload size={15} /> Upload</button></div>
      <Msg error={error} ok={result?.ok && (result.reupload
        ? `Re-uploading ${result.version} (${result.installers.length} services); the previous copy stays in use until it is stored again…`
        : `Uploaded ${result.version} (${result.installers.length} services); checking, then storing…`)}
        warnings={result?.warnings} />
    </div>
  )
}

function OfficeReleases({ st, reload, loadError, onPick, reloadList }) {
  const [error, setError] = useState('')
  const [busy, run] = useAction(setError)
  if (!st) return loadError ? <Msg error={loadError} /> : <Empty>Loading…</Empty>
  if (!st.url) {
    return (
      <Empty title="Not connected to an office" action={can('admin') &&
        <button onClick={() => navigate('/dashboard/settings#catalog')}><Globe size={15} /> Connect in Settings</button>}>
        Enter the office Software Manager address and the branch key in Settings → Release catalog.
        New releases uploaded at the office then download here by themselves.
      </Empty>
    )
  }
  const items = st.available || []
  const prog = st.progress || {}
  const check = () => run(async () => { setError(''); await api('/api/central/check', { method: 'POST' }); reload(); reloadList() })
  return (
    <div>
      <div className="row between center wrap gap">
        <p className="lead" style={{ margin: 0 }}>Office <code>{st.url}</code> · checked every {Math.round(st.poll_sec / 60)} min · last check {fmtTime(st.last_check)}
          {st.auto_download ? ' · new releases download automatically' : ''}. Installing stays a manual step on the Deployments page.</p>
        {can('operator') && <button disabled={busy} onClick={check}><RefreshCw size={15} /> Check now</button>}
      </div>
      <Msg error={error || st.error} />
      {!items.length ? <Empty>{st.error ? 'The office could not be reached.' : 'The office has not published any release yet.'}</Empty> : (
        <div className="mt" style={{ overflowX: 'auto' }}>
          <table className="tbl">
            <thead><tr><th>Version</th><th>Software</th><th>Size</th><th>Published</th><th>Here</th><th /></tr></thead>
            <tbody>
              {items.map((r) => {
                const p = prog[r.version]
                const state = p && p.state !== 'downloaded' ? p.state : r.state
                return (
                  <tr key={r.version}>
                    <td><b>{r.version}</b></td>
                    <td>{r.software_name}</td>
                    <td className="num">{fmtBytes(r.size)}</td>
                    <td>{fmtTime(r.uploaded_at)}</td>
                    <td>
                      {state === 'downloading' && p?.total ? <Progress done={p.done} total={p.total} /> : <Badge value={state} />}
                      {state === 'failed' && p?.error && <div className="sub bad-text">{p.error}</div>}
                      {state === 'conflict' && <div className="sub bad-text">A different release {r.version} is stored here</div>}
                      {state === 'update' && <div className="sub">Re-uploaded at the office {fmtTime(r.uploaded_at)}; the copy here is from {fmtTime(r.local_uploaded_at)}</div>}
                    </td>
                    <td className="actions">
                      {state === 'downloaded' && <button className="sm" onClick={() => onPick(r.version)}>View</button>}
                      {can('operator') && ['available', 'failed', 'update'].includes(state) && (
                        <button className="sm" disabled={busy} onClick={() => run(async () => {
                          setError('')
                          await api(`/api/central/${encodeURIComponent(r.version)}/download`, { method: 'POST' })
                          reload()
                        })}><Download size={14} /> {state === 'update' ? 'Get update' : 'Download'}</button>
                      )}
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}

function AddRelease({ onPick, reloadList }) {
  const [central, reloadCentral, centralError] = usePoll(() => api('/api/central'), 5000)
  const pending = (central?.available || []).filter((r) => r.state !== 'downloaded').length
  const [tab, setTab] = useState(() => (central?.url ? 'office' : 'path'))
  return (
    <Card title="Add a release" description="Three ways to bring a release to this Software Manager. Each is checked, then stored here.">
      <Tabs value={tab} onChange={setTab} items={[
        { id: 'path', label: 'Release path', icon: FolderOpen },
        { id: 'office', label: 'From the office', icon: Globe, count: pending },
        { id: 'file', label: 'YML file', icon: Upload },
      ]} />
      <div className="mt-lg">
        {tab === 'path' && <ReleasePath onPick={onPick} reloadList={reloadList} />}
        {tab === 'office' && <OfficeReleases st={central} reload={reloadCentral} loadError={centralError} onPick={onPick} reloadList={reloadList} />}
        {tab === 'file' && <UploadFile onPick={onPick} reloadList={reloadList} />}
      </div>
    </Card>
  )
}

function PrereqPackages({ list }) {
  const [src] = usePoll(() => api('/api/release-source'), 10000)
  const [bundles, reload, loadError] = usePoll(() => api('/api/bundles'), 4000)
  const [prereq, setPrereq] = useState('')
  const [picked, setPicked] = useState([])
  const [error, setError] = useState('')
  const [ok, setOk] = useState('')
  const [busy, run] = useAction(setError)
  const op = can('operator')
  const pkgs = src?.prerequisites || []
  const ready = (list || []).filter((r) => r.status === 'downloaded')
  const toggle = (v) => setPicked((p) => (p.includes(v) ? p.filter((x) => x !== v) : [...p, v]))

  const build = () => run(async () => {
    setError('')
    setOk('')
    const versions = picked.filter((v) => ready.some((r) => r.version === v))
    const res = await api('/api/bundles', { method: 'POST', body: { prereq, versions } })
    setOk(`Building ${res.name}…`)
    reload()
  })

  return (
    <Card title="Prerequisites package" description="One tar for a site without a network link: the installer plus the releases you tick.">
      <p className="lead">Contains <code>prereq.sh</code>, Docker setup, sm-agent, sm-logger and Software Manager from
        <code>pre_requisite_installer/</code> in the release path, plus the stored releases you choose. At the site run
        <code>tar -xf &lt;package&gt;.tar</code> and <code>sudo bash prereq.sh --manager …</code>; the releases are then ready to install.</p>
      <div className="grid2">
        <label className="field">
          <span className="field-label">Installer</span>
          <select value={prereq} onChange={(e) => setPrereq(e.target.value)}>
            <option value="">{pkgs.length ? 'Select an installer…' : 'No installer in the release path'}</option>
            {pkgs.map((p) => <option key={p.path} value={p.path}>{p.path} · {fmtBytes(p.size)}</option>)}
          </select>
        </label>
        <div className="field">
          <span className="field-label">Releases to include</span>
          <div className="row gap wrap" style={{ minHeight: 32, alignItems: 'center', columnGap: 16 }}>
            {ready.length ? ready.map((r) => (
              <label key={r.version} className="check">
                <input type="checkbox" disabled={!op} checked={picked.includes(r.version)} onChange={() => toggle(r.version)} />
                {r.software_name} {r.version}
              </label>
            )) : <span className="muted small">No stored releases yet.</span>}
          </div>
        </div>
      </div>
      {op && <div className="mt"><button className="primary" disabled={busy || !prereq || !pkgs.some((p) => p.path === prereq)} onClick={build}>
        <Package size={15} /> Build package</button></div>}
      <Msg error={error || loadError} ok={ok} />
      {bundles?.length > 0 && (
        <div className="mt-lg" style={{ overflowX: 'auto' }}>
          <table className="tbl">
            <thead><tr><th>Package</th><th>Releases</th><th>Size</th><th>Created</th><th /></tr></thead>
            <tbody>
              {bundles.map((b) => (
                <tr key={b.name}>
                  <td><span className="mono small">{b.name}</span></td>
                  <td>{b.releases.join(', ') || '—'}</td>
                  <td className="num">{b.building ? <Badge value="building" /> : fmtBytes(b.size)}</td>
                  <td>{fmtTime(b.created)}</td>
                  <td className="actions">
                    {op && !b.building && <button className="sm" disabled={busy}
                      onClick={() => run(() => download(`/api/bundles/${encodeURIComponent(b.name)}`, b.name))}>
                      <Download size={14} /> Download</button>}
                    {can('admin') && !b.building && <button className="icon danger" disabled={busy} title="Delete package" aria-label="Delete package"
                      onClick={async () => {
                        if (!await confirmDialog({ title: 'Delete package?', message: b.name, confirmLabel: 'Delete', danger: true })) return
                        run(async () => { await api(`/api/bundles/${encodeURIComponent(b.name)}`, { method: 'DELETE' }); reload() })
                      }}>
                      <Trash2 size={15} /></button>}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </Card>
  )
}

export default function Release() {
  const [list, reload, listError] = usePoll(() => api('/api/releases'), 4000)
  const [selected, setSelected] = useState('')
  const pick = (v) => {
    setSelected(v)
    setTimeout(() => document.getElementById('release-detail')?.scrollIntoView({ behavior: 'smooth', block: 'start' }), 50)
  }

  return (
    <div className="stack">
      <PageHeader title="Releases"
        description="Bring releases to this Software Manager, check them and keep them ready to install. Installing happens on the Deployments page." />
      <AddRelease onPick={pick} reloadList={reload} />
      <Card flush title="Stored releases" description="Click a release to see its services and checks.">
        <Msg error={listError} />
        {!list?.length ? <Empty title={listError ? 'Releases could not be loaded' : 'No releases yet'}>
          {listError ? '' : 'Add one above from the release path, the office or a YML file.'}</Empty> : (
          <Pager rows={list}>{(rows) => (
            <table className="tbl hover">
              <thead><tr><th>Version</th><th>Software</th><th>Status</th><th>Progress</th><th>Uploaded</th><th>Used by</th></tr></thead>
              <tbody>
                {rows.map((r) => (
                  <tr key={r.version} onClick={() => pick(r.version)} className={selected === r.version ? 'sel' : ''}
                    tabIndex={0} onKeyDown={(e) => e.key === 'Enter' && pick(r.version)}>
                    <td><b>{r.version}</b></td>
                    <td>{r.software_name}</td>
                    <td><Badge value={r.reuploading ? 're-uploading' : r.status} />
                      {r.revision > 1 && <div className="sub">revision {r.revision}</div>}
                      {r.progress?.reupload_error && <div className="sub bad-text" title={r.progress.reupload_error}>re-upload failed</div>}</td>
                    <td>{r.progress?.phase && r.status !== 'downloaded'
                      ? <Progress done={r.progress.done ?? 0} total={r.progress.total ?? 0} label={`${r.progress.phase} ${r.progress.done ?? 0}/${r.progress.total ?? 0}`} />
                      : <span className="muted">—</span>}</td>
                    <td>{fmtTime(r.uploaded_at)}<div className="sub">{r.uploaded_by}</div></td>
                    <td>{r.used_by?.join(', ') || <span className="muted">—</span>}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}</Pager>
        )}
      </Card>
      {selected && <div id="release-detail"><ReleaseDetail key={selected} version={selected} onClose={() => setSelected('')}
        onDeleted={() => { setSelected(''); reload() }} /></div>}
      <PrereqPackages list={list} />
    </div>
  )
}
