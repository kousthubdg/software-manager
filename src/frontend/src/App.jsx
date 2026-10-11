import { useEffect, useState } from 'react'
import { AlertCircle, Bell, Boxes, FileText, LogOut, Menu as MenuIcon, Package, Server, Settings as Cog } from 'lucide-react'
import { api, session } from './api.js'
import { OverlayHost, Popover, ago, navigate, usePoll } from './ui.jsx'
import Login from './pages/Login.jsx'
import Release from './pages/Release.jsx'
import Nodes from './pages/Nodes.jsx'
import Deployment from './pages/Deployment.jsx'
import Logs from './pages/Logs.jsx'
import Settings from './pages/Settings.jsx'

const NAV = [
  { path: '/dashboard/release', label: 'Releases', icon: Package, page: Release, group: 'Operate' },
  { path: '/dashboard/deployment-map', label: 'Deployments', icon: Boxes, page: Deployment, group: 'Operate' },
  { path: '/dashboard/nodes', label: 'Nodes', icon: Server, page: Nodes, group: 'Operate' },
  { path: '/dashboard/logs', label: 'Logs', icon: FileText, page: Logs, group: 'Observe' },
  { path: '/dashboard/settings', label: 'Settings', icon: Cog, page: Settings, group: 'Configure' },
]

function usePath() {
  const [path, setPath] = useState(window.location.pathname)
  useEffect(() => {
    const on = () => setPath(window.location.pathname)
    window.addEventListener('popstate', on)
    return () => window.removeEventListener('popstate', on)
  }, [])
  return path
}

function Alerts() {
  const [alerts] = usePoll(() => api('/api/alerts'), 15000)
  const n = alerts?.length || 0
  return (
    <Popover className="alerts-pop" trigger={({ toggle, open }) => (
      <button className="icon bell" onClick={toggle} aria-expanded={open} title="Alerts" aria-label={`Alerts (${n})`}>
        <Bell size={17} />
        {n > 0 && <span className="dot">{n}</span>}
      </button>
    )}>{() => (
      <>
        <div className="menu-head"><b>Alerts</b><span className="muted small">{n ? `${n} active` : 'Everything is running normally'}</span></div>
        {alerts?.map((a) => (
          <div key={a.key} className="alert-item">
            <AlertCircle size={15} />
            <div><div>{a.message}</div><div className="muted small">since {ago(a.first_ts)}</div></div>
          </div>
        ))}
      </>
    )}</Popover>
  )
}

function UserMenu({ me, onSignOut }) {
  return (
    <Popover trigger={({ toggle, open }) => (
      <button className="user-btn" onClick={toggle} aria-expanded={open} aria-label="Account">
        <span className="avatar">{me.username.slice(0, 2)}</span>
        <span className="who"><span>{me.username}</span><small>{me.role}</small></span>
      </button>
    )}>{(close) => (
      <>
        <div className="menu-head"><b>{me.username}</b><span className="muted small">Role: {me.role}</span></div>
        <div className="menu-sep" />
        <button className="menu-item" onClick={() => { close(); navigate('/dashboard/settings#account') }}>
          <Cog size={15} /> Account settings</button>
        <button className="menu-item" onClick={() => { close(); onSignOut() }}><LogOut size={15} /> Sign out</button>
      </>
    )}</Popover>
  )
}

export default function App() {
  const path = usePath()
  const [me, setMe] = useState(session.get())
  const [navOpen, setNavOpen] = useState(false)

  useEffect(() => {
    if (!me && path !== '/login') navigate('/login')
    if (me && (path === '/' || path === '/login' || path === '/dashboard')) navigate('/dashboard/release')
  }, [me, path])

  useEffect(() => {
    if (me) api('/api/auth/me').then((m) => { const s = { ...session.get(), ...m }; session.set(s); setMe(s) }).catch(() => {})
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  useEffect(() => setNavOpen(false), [path])

  const item = NAV.find((n) => path.startsWith(n.path)) || NAV[0]
  useEffect(() => { document.title = me ? `${item.label} · Software Manager` : 'Sign in · Software Manager' }, [item.label, me])

  if (!me || path === '/login') {
    return <><Login onLogin={(s) => { session.set(s); setMe(s); navigate('/dashboard/release') }} /><OverlayHost /></>
  }
  const Page = item.page
  const signOut = () => { session.clear(); setMe(null); navigate('/login') }
  const groups = [...new Set(NAV.map((n) => n.group))]

  return (
    <div className={`shell ${navOpen ? 'nav-open' : ''}`}>
      <aside className="side" aria-label="Main navigation">
        <div className="brand">
          <div className="logo" aria-hidden="true">SM</div>
          <div>
            <div className="brand-name">Software Manager</div>
            <div className="brand-sub">Yellow Metal Platform</div>
          </div>
        </div>
        <nav>
          {groups.map((g) => (
            <div key={g}>
              <div className="nav-label">{g}</div>
              {NAV.filter((n) => n.group === g).map((n) => (
                <a key={n.path} href={n.path} className={item.path === n.path ? 'active' : ''} aria-current={item.path === n.path ? 'page' : undefined}
                  onClick={(e) => { e.preventDefault(); navigate(n.path) }}>
                  <n.icon size={16} /> {n.label}
                </a>
              ))}
            </div>
          ))}
        </nav>
        <div className="side-foot">
          <span>Branch <b>{me.branch || '—'}</b></span>
        </div>
      </aside>
      <div className="scrim" onClick={() => setNavOpen(false)} />
      <main className="main">
        <header className="top">
          <div className="row center gap" style={{ minWidth: 0 }}>
            <button className="icon menu-toggle" aria-label="Open navigation" onClick={() => setNavOpen(true)}><MenuIcon size={18} /></button>
            <nav className="crumbs" aria-label="Breadcrumb">
              <span>{me.branch ? `Branch ${me.branch}` : 'Software Manager'}</span>
              <span className="slash">/</span>
              <b>{item.label}</b>
            </nav>
          </div>
          <div className="row gap center">
            <Alerts />
            <UserMenu me={me} onSignOut={signOut} />
          </div>
        </header>
        <div className="content"><Page /></div>
      </main>
      <OverlayHost />
    </div>
  )
}
