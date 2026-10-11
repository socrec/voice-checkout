// The shopper's page. Why each design choice was made: frontend/DESIGN.md
import { useEffect, useRef, useState } from 'react'
import { createRoot } from 'react-dom/client'
import './style.css'

const Recognition = window.SpeechRecognition || window.webkitSpeechRecognition

// Browser storage can throw in private windows, so every read and write is guarded.
function stored(kind, key, fallback) {
  try {
    return window[kind].getItem(key) ?? fallback
  } catch {
    return fallback
  }
}
function store(kind, key, value) {
  try {
    value == null ? window[kind].removeItem(key) : window[kind].setItem(key, value)
  } catch {}
}

// Kept for this tab, so a reload carries on with the same cart and conversation on the server.
const sessionId = stored('sessionStorage', 'sessionId', null) ?? crypto.randomUUID()
store('sessionStorage', 'sessionId', sessionId)
const RATES = { normal: 0.9, slower: 0.75 } // both a little slower than the browser's default of 1
const GREETING = 'Hello! Press the big button and tell me what you need.'
const TRY = ['What do I usually buy?', 'Add a dozen eggs', "What's in my cart?", "I'm ready to pay"]
const LABEL = { idle: 'Press to talk', listening: 'Listening…', thinking: 'Thinking…' }
const HINT = { idle: 'Then say what you need', listening: 'Press again when you are done', thinking: 'One moment, please' }
// Each mic failure gets its own wording and a next step, so a retry never hears the same sentence twice.
const MIC_ERRORS = {
  'not-allowed': 'The microphone is blocked. Click the microphone icon in the address bar, choose Allow, then press the button again.',
  'no-speech': "I didn't hear anything. Press the big button, then speak.",
  'audio-capture': "I can't find a microphone. Check that one is plugged in, or type below instead.",
}

// The PIN is typed on the keypad and never goes through the model. If the shopper says one out loud anyway,
// it is still hidden on screen.
const maskDigits = (text) => text.replace(/\d(?:[\s-]*\d){3,}/g, '••••')

// Chrome stops speaking after about 15 seconds, so queue one sentence at a time.
// Split only on a space after the full stop, so "$3.99" stays whole.
// speechId drops end events from speech that was cancelled or replaced.
let speechId = 0
function speak(text, rate, onDone) {
  const id = ++speechId
  speechSynthesis.cancel()
  const sentences = text.split(/(?<=[.!?])\s+/)
  sentences.forEach((sentence, i) => {
    const u = new SpeechSynthesisUtterance(sentence)
    u.rate = rate
    if (i === sentences.length - 1) u.onend = u.onerror = () => id === speechId && onDone()
    speechSynthesis.speak(u)
  })
}

function hush() {
  speechId++
  speechSynthesis.cancel()
}

// A rising tone when the mic opens and a falling one when it closes, so nobody has to watch the screen.
let audio
function beep(hz) {
  audio ??= new AudioContext()
  const osc = audio.createOscillator()
  const gain = audio.createGain()
  const t = audio.currentTime
  osc.frequency.value = hz
  gain.gain.setValueAtTime(0.15, t)
  gain.gain.exponentialRampToValueAtTime(0.001, t + 0.15)
  osc.connect(gain).connect(audio.destination)
  osc.start(t)
  osc.stop(t + 0.15)
}


const Mic = () => (
  <svg viewBox="0 0 24 24" aria-hidden="true" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round">
    <rect x="9" y="3" width="6" height="11" rx="3" />
    <path d="M5 11a7 7 0 0 0 14 0M12 18v3" />
  </svg>
)

function App() {
  const [status, setStatus] = useState('idle') // idle | listening | thinking
  const [speaking, setSpeaking] = useState(false)
  const [heard, setHeard] = useState('') // words so far, while listening
  const [turns, setTurns] = useState([{ you: '', shop: GREETING }])
  const [cart, setCart] = useState({ lines: [], total: '$0.00' })
  const [changed, setChanged] = useState(new Set()) // names of lines the last reply added or changed
  const [typed, setTyped] = useState('')
  const [connected, setConnected] = useState(null) // null until we know
  const [aloud, setAloud] = useState(() => stored('localStorage', 'readAloud', 'on') !== 'off')
  const [speed, setSpeed] = useState(() => (stored('localStorage', 'speed', 'normal') === 'slower' ? 'slower' : 'normal'))
  // id of an order held for the family to check; kept for this tab so a reload still hears their answer
  const [waiting, setWaiting] = useState(() => Number(stored('sessionStorage', 'waiting', '')) || null)
  const [paying, setPaying] = useState(null) // the total the shopper agreed to, while the PIN pad is up
  const [contact, setContact] = useState({ name: 'your family', phone: null }) // who checks held orders
  const [pin, setPin] = useState('')
  const [sending, setSending] = useState(false) // a PIN is on its way to the server
  const pinInput = useRef(null)
  const rec = useRef(null)

  useEffect(() => {
    fetch('/api/wallet').then((r) => r.json()).then((w) => setConnected(w.connected), () => {})
  }, [])

  useEffect(() => store('sessionStorage', 'waiting', waiting), [waiting])

  // After a reload, pick up the cart and any open PIN pad from the server.
  useEffect(() => {
    fetch(`/api/session/${sessionId}`)
      .then((r) => (r.ok ? r.json() : Promise.reject(r.status)))
      .then((s) => {
        setCart(s.cart)
        setPaying(s.paying)
        setContact(s.contact)
      }, () => {})
  }, [])

  // The PIN pad takes focus when it opens, so keyboard and screen reader users land on it.
  useEffect(() => {
    if (paying) pinInput.current?.focus()
  }, [paying])

  // One place for every reply that changes the order, payment or hold.
  function update(data) {
    const before = new Map(cart.lines.map((l) => [l.name, l.quantity]))
    setChanged(new Set(data.cart.lines.filter((l) => before.get(l.name) !== l.quantity).map((l) => l.name)))
    setCart(data.cart)
    setPaying(data.paying)
    if (data.waiting) setWaiting(data.waiting)
  }

  async function pay(e) {
    e.preventDefault()
    if (pin.length !== 4 || sending) return
    setSending(true)
    stopVoice()
    try {
      const res = await fetch('/api/pay', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ session_id: sessionId, pin }),
      })
      if (!res.ok) throw new Error(res.status)
      const data = await res.json()
      update(data)
      notice(data.message)
    } catch {
      notice('Sorry, the payment did not go through, and nothing was paid. Please try again.')
    }
    setPin('')
    setSending(false)
  }

  async function notNow() {
    setPin('')
    setPaying(null)
    await fetch('/api/pay/cancel', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ session_id: sessionId }),
    }).catch(() => {})
    notice('All right, nothing was paid. Your order is still here.')
  }

  const typePin = (digits) => setPin(digits.replace(/\D/g, '').slice(0, 4))

  function voice(text) {
    setSpeaking(true)
    speak(text, RATES[speed], () => setSpeaking(false))
  }

  function stopVoice() {
    hush()
    setSpeaking(false)
  }

  function notice(text) {
    setTurns((t) => [...t, { you: '', shop: text }])
    if (aloud) voice(text)
  }

  // Ask every few seconds while the family decides, then say their answer out loud.
  useEffect(() => {
    if (!waiting) return
    const timer = setInterval(async () => {
      const r = await fetch(`/api/orders/${waiting}/status`).catch(() => null)
      const { message } = r?.ok ? await r.json() : {}
      if (message) {
        setWaiting(null)
        notice(message)
      }
    }, 4000)
    return () => clearInterval(timer)
  }, [waiting, aloud])

  function toggleAloud(on) {
    setAloud(on)
    if (!on) stopVoice()
    store('localStorage', 'readAloud', on ? 'on' : 'off')
  }

  function changeSpeed(value) {
    setSpeed(value)
    store('localStorage', 'speed', value)
  }

  async function send(text) {
    stopVoice()
    setHeard('')
    setTurns((t) => [...t, { you: text, shop: '' }])
    setStatus('thinking')
    let reply
    try {
      const res = await fetch('/api/chat', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ session_id: sessionId, text }),
      })
      if (!res.ok) throw new Error(res.status)
      const data = await res.json()
      update(data)
      reply = data.reply
    } catch {
      reply = 'Sorry, something went wrong on our side. Please try again.'
    }
    setTurns((t) => [...t.slice(0, -1), { ...t.at(-1), shop: reply }])
    setStatus('idle')
    if (aloud) voice(reply)
  }

  function talk() {
    if (status === 'listening') return rec.current.stop()
    stopVoice()
    const r = new Recognition()
    r.lang = 'en-US'
    r.interimResults = true
    r.onresult = (e) => {
      const result = e.results[0]
      setHeard(result[0].transcript)
      if (result.isFinal) send(result[0].transcript)
    }
    r.onerror = (e) => e.error !== 'aborted' && notice(MIC_ERRORS[e.error] ?? "Sorry, I didn't catch that. Please try again.")
    r.onend = () => {
      beep(440)
      setStatus((s) => (s === 'listening' ? 'idle' : s))
    }
    rec.current = r
    beep(880)
    r.start()
    setStatus('listening')
  }

  function submit(e) {
    e.preventDefault()
    if (!typed.trim()) return
    send(typed.trim())
    setTyped('')
  }

  const busy = status === 'thinking'
  const latest = turns.at(-1)
  const earlier = turns.slice(0, -1)
  const said = heard || latest.you
  const items = cart.lines.reduce((n, l) => n + l.quantity, 0)

  return (
    <>
      <header>
        <h1>Voice Checkout</h1>
        <a className="header-total" href="#order">
          {items} {items === 1 ? 'item' : 'items'}, {cart.total}
        </a>
      </header>

      <main>
        <section className="talk-col" aria-label="Talk to the shop">
          {Recognition ? (
            <button className={`talk ${status}`} onClick={talk} disabled={busy} autoFocus>
              <Mic />
              <span>{LABEL[status]}</span>
              <small>{HINT[status]}</small>
            </button>
          ) : (
            <p className="note">Talking works in Google Chrome on a computer. You can type below instead.</p>
          )}

          <div className="exchange" aria-busy={busy}>
            {said && (
              <p className="you">
                <span className="who">You said</span>“{maskDigits(said)}”
              </p>
            )}
            <p className="shop" aria-live="polite">
              {busy ? 'Thinking…' : latest.shop}
            </p>
            <div className="row">
              <button onClick={() => voice(latest.shop)} disabled={busy || !latest.shop}>
                Say that again
              </button>
              {speaking && <button onClick={stopVoice}>Stop reading</button>}
            </div>
          </div>

          {paying && (
            <form className="checkout" onSubmit={pay} aria-labelledby="pay-title">
              <h2 id="pay-title">Pay {paying}</h2>
              <p>Check your order, then type your four-digit PIN. Don't say it out loud.</p>
              <label htmlFor="pin">Your PIN</label>
              <input
                id="pin"
                ref={pinInput}
                type="password"
                inputMode="numeric"
                autoComplete="off"
                maxLength={4}
                value={pin}
                onChange={(e) => typePin(e.target.value)}
                disabled={sending}
              />
              <div className="keypad">
                {['1', '2', '3', '4', '5', '6', '7', '8', '9'].map((d) => (
                  <button key={d} type="button" onClick={() => typePin(pin + d)} disabled={sending}>
                    {d}
                  </button>
                ))}
                <button type="button" onClick={() => setPin('')} disabled={sending}>
                  Clear
                </button>
                <button type="button" onClick={() => typePin(pin + '0')} disabled={sending}>
                  0
                </button>
                <button type="button" onClick={() => setPin(pin.slice(0, -1))} disabled={sending} aria-label="Delete last digit">
                  ⌫
                </button>
              </div>
              <div className="row">
                <button className="pay" disabled={pin.length !== 4 || sending}>
                  {sending ? 'Paying…' : `Pay ${paying}`}
                </button>
                <button type="button" onClick={notNow} disabled={sending}>
                  Not now
                </button>
              </div>
            </form>
          )}

          <h2 id="try-title">You can say</h2>
          <ul className="try" aria-labelledby="try-title">
            {TRY.map((t) => (
              <li key={t}>
                <button className="chip" onClick={() => send(t)} disabled={status !== 'idle'}>
                  “{t}”
                </button>
              </li>
            ))}
          </ul>

          <form onSubmit={submit}>
            <label htmlFor="typed">Or type what you need</label>
            <div className="row">
              <input id="typed" value={typed} onChange={(e) => setTyped(e.target.value)} disabled={busy} autoComplete="off" />
              <button disabled={busy}>Send</button>
            </div>
          </form>

          {earlier.length > 0 && (
            <details>
              <summary>Earlier in this conversation</summary>
              <ol className="log">
                {earlier.map((t, i) => (
                  <li key={i}>
                    {t.you && (
                      <p className="you">
                        <span className="who">You</span>
                        {maskDigits(t.you)}
                      </p>
                    )}
                    <p>
                      <span className="who">Shop</span>
                      {t.shop}
                    </p>
                  </li>
                ))}
              </ol>
            </details>
          )}
        </section>

        <aside id="order" aria-labelledby="order-title">
          <h2 id="order-title">Your order</h2>
          {cart.lines.length === 0 ? (
            <p className="muted">Nothing yet. Ask for something and it appears here.</p>
          ) : (
            <ul className="lines">
              {cart.lines.map((l) => (
                <li key={l.name} className={changed.has(l.name) ? 'changed' : undefined}>
                  <span className="qty">{l.quantity} ×</span>
                  <span>
                    {l.name}
                    {changed.has(l.name) && <span className="tag">Updated</span>}
                  </span>
                  <span className="price">{l.price}</span>
                </li>
              ))}
            </ul>
          )}
          <p className="total">
            <span>Total</span>
            <span>{cart.total}</span>
          </p>
          {waiting && (
            <div className="held-note">
              <p className="small">
                Order {waiting} is waiting for {contact.name} to check it. Nothing is paid yet.
              </p>
              {contact.phone && (
                <a className="call" href={`tel:${contact.phone.replace(/[^\d+]/g, '')}`}>
                  Call {contact.name}: {contact.phone}
                </a>
              )}
            </div>
          )}
          <p className="muted small">Nothing is paid until you type your PIN on the screen.</p>
          {connected === true && <p className="small">✓ Pays with your saved PayPal account</p>}
          {connected === false && <p className="small">PayPal is not set up yet. Ask your family to connect it.</p>}
        </aside>
      </main>

      <footer>
        <label className="toggle">
          <input type="checkbox" checked={aloud} onChange={(e) => toggleAloud(e.target.checked)} />
          Read replies aloud
        </label>
        <label className="toggle">
          Reading speed
          <select value={speed} onChange={(e) => changeSpeed(e.target.value)}>
            <option value="normal">Normal</option>
            <option value="slower">Slower</option>
          </select>
        </label>
        <details className="family">
          <summary>For family</summary>
          <p>Connecting PayPal, every order and its scam check, and paused payments are on the family page. Open it from your family link.</p>
        </details>
      </footer>
    </>
  )
}

createRoot(document.getElementById('root')).render(<App />)
