import { useState } from 'react'
import { api } from '../api.js'
import { Field, Msg } from '../ui.jsx'

export default function Login({ onLogin }) {
  const [username, setU] = useState('admin')
  const [password, setP] = useState('')
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)

  const submit = async (e) => {
    e.preventDefault()
    setBusy(true)
    setError('')
    try {
      onLogin(await api('/api/auth/login', { method: 'POST', body: { username, password } }))
    } catch (err) {
      setError(err.message)
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="login-bg">
      <form className="login" onSubmit={submit}>
        <div className="head">
          <div className="logo big" aria-hidden="true">SM</div>
          <h1>Sign in to Software Manager</h1>
          <p className="muted">Yellow Metal Platform · branch deployment</p>
        </div>
        <Field label="Username">
          <input value={username} onChange={(e) => setU(e.target.value)} autoComplete="username" />
        </Field>
        <Field label="Password">
          <input type="password" value={password} onChange={(e) => setP(e.target.value)} autoFocus autoComplete="current-password" />
        </Field>
        <Msg error={error} />
        <button className="primary wide" disabled={busy || !username || !password}>{busy ? 'Signing in…' : 'Sign in'}</button>
        <p className="note">First sign-in: the initial admin password is in the manager's state folder
          (<code>initial-admin-password</code>) unless <code>SM_ADMIN_PASSWORD</code> was set.</p>
      </form>
    </div>
  )
}
