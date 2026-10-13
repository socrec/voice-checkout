// Family view: every order with the scam check that ran before payment, and the held ones to approve or reject.
// AG Grid Community, so no licence key.
import { useEffect, useState } from 'react'
import { createRoot } from 'react-dom/client'
import { AgGridReact } from 'ag-grid-react'
import {
  CellStyleModule,
  ClientSideRowModelModule,
  DateFilterModule,
  ModuleRegistry,
  NumberFilterModule,
  QuickFilterModule,
  RowApiModule,
  RowAutoHeightModule,
  RowStyleModule,
  TextFilterModule,
  ValidationModule,
  themeQuartz,
} from 'ag-grid-community'
import './caregiver.css'

// Only the features this page uses, which keeps the download small. While developing, the validation module
// names any feature that is missing from this list in the console. Production builds drop it.
ModuleRegistry.registerModules([
  CellStyleModule, // the Total column's right alignment
  ClientSideRowModelModule,
  TextFilterModule,
  NumberFilterModule,
  DateFilterModule,
  QuickFilterModule,
  RowAutoHeightModule,
  RowStyleModule,
  RowApiModule,
  ...(import.meta.env.DEV ? [ValidationModule] : []),
])

const RISK = { high: { label: 'High', rank: 3 }, medium: { label: 'Medium', rank: 2 }, low: { label: 'Low', rank: 1 } }
const riskLabel = (risk) => RISK[risk]?.label ?? 'Not checked' // orders from before the scam check have no risk
const PAYMENT = {
  HELD: 'Waiting for you',
  REJECTED: 'Stopped',
  PAYING: 'Paying…',
  COMPLETED: 'Paid',
  FAILED: 'Declined',
  PENDING: 'Pending',
  REFUNDED: 'Refunded',
}
const dollars = (cents) => `$${(cents / 100).toFixed(2)}`
const itemList = (o) => o.items.map((l) => `${l.quantity} × ${l.name}`).join(', ')

// The family link is caregiver.html#key=FAMILY_KEY. A hash never leaves the browser, so the key stays out of server logs.
// It's also kept for this tab, because PayPal's return trip after Connect PayPal lands here without the hash.
// Not localStorage: a key left on the shopper's own computer could let a caller talk her into approving her own payment.
const key = (() => {
  // URLSearchParams reads "+" as a space, and Render's generated key is base64, so keep "+" literal.
  const fromLink = new URLSearchParams(location.hash.slice(1).replaceAll('+', '%2B')).get('key')
  try {
    if (fromLink) sessionStorage.setItem('familyKey', fromLink)
    return fromLink ?? sessionStorage.getItem('familyKey') ?? ''
  } catch {
    return fromLink ?? ''
  }
})()
const api = (path, method = 'GET') => fetch(path, { method, headers: { 'X-Family-Key': key } })

const theme = themeQuartz.withParams({
  accentColor: '#0a4f96',
  fontFamily: 'system-ui, sans-serif',
  fontSize: 16,
  headerFontSize: 16,
  headerFontWeight: 700,
  rowVerticalPaddingScale: 1.4,
  borderColor: '#d6d3cc',
  wrapperBorderRadius: 8,
})

// Words and colour together, so the level never depends on seeing red.
const Risk = ({ value }) => <span className={`risk ${value ?? 'none'}`}>{riskLabel(value)}</span>

const Signals = ({ data }) =>
  data.signals.length ? (
    <ul className="signals">
      {data.signals.map((s) => (
        <li key={s.code}>{s.reason}</li>
      ))}
    </ul>
  ) : (
    <span className="muted">None</span>
  )

// Two clicks, since a refund can't be taken back.
function Refund({ data, context }) {
  const [sure, setSure] = useState(false)
  if (data.status !== 'COMPLETED') return null
  return (
    <button className="cell-button" onClick={() => (sure ? context.act(data.id, 'refund') : setSure(true))}>
      {sure ? `Confirm refund of ${dollars(data.total_cents)}` : 'Refund'}
    </button>
  )
}

const columns = [
  { field: 'id', headerName: 'Order', width: 105 },
  {
    headerName: 'Placed',
    valueGetter: (p) => new Date(p.data.created),
    cellDataType: 'dateTime',
    valueFormatter: (p) => p.value.toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' }),
    sort: 'desc',
    width: 200,
  },
  {
    field: 'risk',
    cellRenderer: Risk,
    comparator: (a, b) => (RISK[a]?.rank ?? 0) - (RISK[b]?.rank ?? 0),
    filterValueGetter: (p) => riskLabel(p.data.risk),
    width: 150,
  },
  {
    headerName: 'Scam signals',
    valueGetter: (p) => p.data.signals.map((s) => s.reason).join(' '),
    cellRenderer: Signals,
    wrapText: true,
    autoHeight: true,
    sortable: false,
    flex: 2,
    minWidth: 280,
  },
  {
    headerName: 'Items',
    valueGetter: (p) => itemList(p.data),
    wrapText: true,
    autoHeight: true,
    flex: 1.5,
    minWidth: 220,
  },
  {
    headerName: 'Total',
    valueGetter: (p) => p.data.total_cents / 100, // dollars, so the number filter takes "50", not "5000"
    valueFormatter: (p) => `$${p.value.toFixed(2)}`,
    type: 'rightAligned',
    width: 120,
  },
  {
    field: 'status',
    headerName: 'Payment',
    valueFormatter: (p) => PAYMENT[p.value] ?? p.value,
    filterValueGetter: (p) => PAYMENT[p.data.status] ?? p.data.status,
    width: 150,
  },
  { headerName: 'Undo', cellRenderer: Refund, sortable: false, filter: false, width: 230 },
]

function App() {
  const [orders, setOrders] = useState(null) // null while loading
  const [failed, setFailed] = useState(false)
  const [redOnly, setRedOnly] = useState(false)
  const [search, setSearch] = useState('')
  const [shown, setShown] = useState(0)
  const [denied, setDenied] = useState(!key)
  const [deciding, setDeciding] = useState(null) // order id while approve, reject or refund runs
  const [problem, setProblem] = useState('')
  const [connected, setConnected] = useState(null) // is PayPal saved? null until we know
  const [setupProblem, setSetupProblem] = useState('')

  function load() {
    api('/api/orders')
      .then((r) => (r.status === 401 ? setDenied(true) : r.ok ? r.json().then(setOrders) : Promise.reject(r.status)))
      .then(
        () => setFailed(false),
        () => {
          setOrders((o) => o ?? [])
          setFailed(true)
        },
      )
  }
  // ponytail: polls every 5 seconds so a held order shows up while this page is open; add a push or text
  // message when the family should hear about it with the page closed
  useEffect(() => {
    if (!key) return
    load()
    fetch('/api/wallet').then((r) => r.json()).then((w) => setConnected(w.connected), () => {})
    const timer = setInterval(load, 5000)
    return () => clearInterval(timer)
  }, [])

  async function act(id, action) {
    setDeciding(id)
    setProblem('')
    const r = await api(`/api/orders/${id}/${action}`, 'POST').catch(() => null)
    const body = (await r?.json().catch(() => null)) ?? {}
    if (!r?.ok) setProblem(body.detail ?? "That didn't work. Please try again.")
    else if (body.status === 'FAILED') setProblem(`PayPal turned order ${id} down. Nothing was paid.`)
    setDeciding(null)
    load()
  }

  // Approve once on PayPal; PayPal then sends the browser back here.
  async function connectPayPal() {
    setSetupProblem('')
    const r = await api('/api/vault/setup', 'POST').catch(() => null)
    if (r?.ok) location.href = (await r.json()).approve_url
    else setSetupProblem("PayPal setup didn't start. Please try again in a minute.")
  }

  if (denied)
    return (
      <main>
        <h1>Family view</h1>
        <p className="error">This page opens from the family link, which ends in a key. Ask whoever set up Voice Checkout for it.</p>
      </main>
    )

  const all = orders ?? []
  const redFlags = all.filter((o) => o.risk === 'high')
  const held = all.filter((o) => o.status === 'HELD')

  return (
    <main>
      <header>
        <div>
          <h1>Family view</h1>
          <p className="muted">Every order, with the scam check that ran before it was paid.</p>
        </div>
        <a href="/">Back to shopping</a>
      </header>

      {connected === false && (
        <section className="setup" aria-labelledby="setup-title">
          <h2 id="setup-title">Connect PayPal</h2>
          <p>Approve once on PayPal. After that, shopping is paid with a PIN, and risky orders wait here for you.</p>
          <button onClick={connectPayPal}>Connect PayPal</button>
          {setupProblem && (
            <p className="error" role="alert">
              {setupProblem}
            </p>
          )}
        </section>
      )}

      {held.length > 0 && (
        <section className="held" aria-labelledby="held-title">
          <h2 id="held-title">Waiting for your answer ({held.length})</h2>
          {held.map((o) => (
            <article key={o.id} className={`card ${o.risk}`}>
              <p className="card-head">
                <Risk value={o.risk} /> Order {o.id}, {dollars(o.total_cents)}
              </p>
              <p>{itemList(o)}</p>
              {o.reason && <p>The shopper was told: “{o.reason}”</p>}
              <ul className="signals">
                {o.signals.map((s) => (
                  <li key={s.code}>{s.reason}</li>
                ))}
              </ul>
              <div className="decide">
                <button className="approve" disabled={deciding === o.id} onClick={() => act(o.id, 'approve')}>
                  Approve and pay {dollars(o.total_cents)}
                </button>
                <button className="reject" disabled={deciding === o.id} onClick={() => act(o.id, 'reject')}>
                  Reject
                </button>
              </div>
            </article>
          ))}
        </section>
      )}
      {problem && (
        <p className="error" role="alert">
          {problem}
        </p>
      )}

      <div className="toolbar">
        <label className="toggle">
          <input type="checkbox" checked={redOnly} onChange={(e) => setRedOnly(e.target.checked)} />
          Red flags only ({redFlags.length})
        </label>
        <label className="search">
          Search
          <input type="search" value={search} onChange={(e) => setSearch(e.target.value)} placeholder="gift card, bail, milk…" />
        </label>
        <button onClick={load}>Refresh</button>
        <span className="count" aria-live="polite">
          Showing {shown} of {all.length} orders
        </span>
      </div>
      {failed && <p className="error">Couldn't load orders. Press Refresh to try again.</p>}

      <div className="grid">
        <AgGridReact
          theme={theme}
          rowData={redOnly ? redFlags : all}
          columnDefs={columns}
          defaultColDef={{ filter: true, minWidth: 100 }}
          getRowId={(p) => String(p.data.id)}
          context={{ act }}
          rowClassRules={{ 'red-flag': (p) => p.data.risk === 'high' }}
          quickFilterText={search}
          loading={orders === null}
          overlayNoRowsTemplate={redOnly ? 'No red-flag orders.' : 'No orders yet.'}
          onModelUpdated={(e) => setShown(e.api.getDisplayedRowCount())}
          ensureDomOrder
          enableCellTextSelection
        />
      </div>
    </main>
  )
}

createRoot(document.getElementById('root')).render(<App />)
