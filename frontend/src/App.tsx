import { useCallback, useEffect, useRef, useState, type FormEvent, type MouseEvent } from 'react'
import L from 'leaflet'
import { MapContainer, Marker, Polyline, Popup, TileLayer, useMap } from 'react-leaflet'
import 'leaflet/dist/leaflet.css'
import './App.css'

const API = import.meta.env.VITE_API_BASE_URL ?? 'http://127.0.0.1:8000'
type Tab = 'Overview' | 'Donations' | 'Recipients' | 'Verification' | 'Impact' | 'Notifications'
type User = { id: string; email: string; name: string; role: 'donor' | 'recipient' | 'admin' }
type Portion = { id: string; recipient_id: string; recipient_name: string; recipient_contact?: string; recipient_address?: string; destination_lat?: number | null; destination_lon?: number | null; quantity_kg: number; status: string; created_at?: string; pickup_at?: string | null; received_at?: string | null; distance_km?: number }
type Donation = { id: string; donor_name: string; food_name: string; category: string; quantity_kg: number; reserved_quantity_kg?: number; delivered_quantity_kg?: number; available_quantity_kg?: number; current_listing_quantity_kg?: number; current_listing_view?: boolean; current_listing_expired?: boolean; prepared_at: string; expires_at: string; storage: string; temperature_c?: number; pickup_address: string; lat: number; lon: number; status: string; portions: Portion[]; timeline: { event_type: string; detail: string; created_at: string; portion_id?: string | null }[]; recommendations?: Portion[]; unallocated_kg?: number; request_eligibility?: { eligible: boolean; reason: string; distance_km: number | null } }
function donationSection(d: Donation): 'current' | 'completed' | 'expired' {
  const delivered = donationDeliveredKg(d)
  if (delivered >= d.quantity_kg - 0.01) return 'completed'
  if (new Date(d.expires_at).getTime() <= Date.now()) return 'expired'
  return 'current'
}
function donationDeliveredKg(d: Donation) {
  return d.delivered_quantity_kg ?? d.portions?.filter(p => p.status === 'received').reduce((sum, p) => sum + p.quantity_kg, 0) ?? 0
}
function donationRemainingKg(d: Donation) { return Math.max(0, d.quantity_kg - donationDeliveredKg(d)) }
function donationAvailableKg(d: Donation) {
  return d.available_quantity_kg ?? Math.max(0, donationRemainingKg(d) - (d.reserved_quantity_kg ?? 0))
}
function remainingListingView(d: Donation): Donation {
  return { ...d, current_listing_view: true, current_listing_expired: donationSection(d) === 'expired', current_listing_quantity_kg: donationRemainingKg(d), portions: d.portions.filter(p => ['offered','accepted','picked_up'].includes(p.status)) }
}
type Recipient = { id: string; name: string; contact: string; address: string; capacity_kg: number; categories: string[]; storage: string[]; hours: string; verification_status: string; evidence: string; reference_id: string; created_at: string; distance_km?: number; documents?: PrivateDocument[]; verification_report: { workflow?: string; checks?: { agent: string; status: string; detail: string }[]; flags?: string[]; registry?: { status: string; detail: string }; recommendation: string; document_details?: string; registration_reference?: string; official_record_check?: string } }
type Impact = { donations_listed: number; completed_donations: number; meals_redistributed: number; quantity_redistributed_kg: number; estimated_food_waste_avoided_kg: number; estimated_co2e_avoided_kg: number; successful_rescues: number; at_risk_donations: number }
type NoticeItem = { id: string; kind: string; title: string; body: string; read_at: string | null; created_at: string }
type PrivateDocument = { id: string; filename: string; content_type: string; sha256: string; created_at: string }

async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const headers = new Headers(init?.headers)
  if (!(init?.body instanceof FormData)) headers.set('Content-Type', 'application/json')
  if (sessionToken) headers.set('Authorization', `Bearer ${sessionToken}`)
  let response: Response
  try {
    response = await fetch(`${API}${path}`, { ...init, headers, cache: init?.method ? undefined : 'no-store', signal: init?.signal ?? AbortSignal.timeout(20000) })
  } catch (error) {
    if (error instanceof DOMException && error.name === 'TimeoutError') throw new Error('The AnnSetu API did not respond in time. Check that the backend and its Supabase database connection are available, then retry.')
    if (error instanceof TypeError) throw new Error(`Could not reach the AnnSetu API at ${API}. Check that the backend is running and that its Supabase database connection is available.`)
    throw error
  }
  const data = await response.json().catch(() => ({}))
  if (!response.ok) throw new Error(data.detail || `Request failed (${response.status})`)
  return data as T
}
const dateLabel = (date: string) => new Date(date).toLocaleString([], { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' })
const statusLabel = (s: string) => s.replaceAll('_', ' ')
function donationDisplayStatus(d: Donation) {
  const deliveredKg = d.delivered_quantity_kg ?? (d.portions?.filter(p => p.status === 'received').reduce((sum, p) => sum + p.quantity_kg, 0) ?? 0)
  if (donationSection(d) === 'completed') return { key: 'completed', label: 'completed' }
  if (donationSection(d) === 'expired') return { key: 'expired', label: 'expired' }
  if (deliveredKg > 0) return { key: 'partially_delivered', label: 'partially delivered' }
  if (d.status === 'at_risk' && d.portions?.some(p => ['accepted', 'picked_up'].includes(p.status))) return { key: 'in_progress', label: 'handover in progress' }
  return { key: d.status, label: statusLabel(d.status) }
}
let sessionToken: string | null = localStorage.getItem('annsetu-token')
let sessionRole: User['role'] = 'donor'
let sessionRecipientId: string | null = null
let sessionRecipientApproved = false

function App() {
  const [tab, setTab] = useState<Tab>('Overview')
  const [donations, setDonations] = useState<Donation[]>([])
  const [recipients, setRecipients] = useState<Recipient[]>([])
  const [impact, setImpact] = useState<Impact | null>(null)
  const [loading, setLoading] = useState(true)
  const [busy, setBusy] = useState(false)
  const [notice, setNotice] = useState<{ kind: 'ok' | 'error'; text: string } | null>(null)
  const [deliveryCodes, setDeliveryCodes] = useState<Record<string, string>>({})
  const [user, setUser] = useState<User | null>(null)
  const [authReady, setAuthReady] = useState(false)
  const [notifications, setNotifications] = useState<NoticeItem[]>([])
  const refreshInFlight = useRef<Promise<void> | null>(null)
  const refresh = useCallback(() => {
    if (refreshInFlight.current) return refreshInFlight.current
    const task = (async () => {
      setLoading(true)
      async function settled<T>(request: Promise<T>) {
        try { return { status: 'fulfilled' as const, value: await request } }
        catch (reason) { return { status: 'rejected' as const, reason } }
      }
      const role = user?.role
      const needsDonations = tab === 'Overview' || tab === 'Donations' || (tab === 'Recipients' && role === 'donor')
      const needsRecipients = tab === 'Recipients' || tab === 'Verification' || role === 'recipient' || role === 'admin'
      const needsImpact = tab === 'Impact' || (tab === 'Overview' && role === 'donor')
      const needsNotifications = tab === 'Notifications'
      // Start every request needed by this screen together. Waiting for the
      // first pair before starting impact/notifications added a full network
      // round trip to dashboards that need those resources.
      const [donationResult, recipientResult, impactResult, notificationResult] = await Promise.all([
        needsDonations ? settled(api<Donation[]>('/api/donations')) : Promise.resolve(null),
        needsRecipients ? settled(api<Recipient[]>('/api/recipients')) : Promise.resolve(null),
        needsImpact ? settled(api<Impact>('/api/impact')) : Promise.resolve(null),
        needsNotifications ? settled(api<{items:NoticeItem[]}>('/api/notifications')) : Promise.resolve(null),
      ])
      const failures: string[] = []
      if (donationResult?.status === 'fulfilled') setDonations(donationResult.value)
      else if (donationResult) failures.push(`donations: ${donationResult.reason instanceof Error ? donationResult.reason.message : 'request failed'}`)
      if (recipientResult?.status === 'fulfilled') {
        const rows = recipientResult.value
        setRecipients(rows)
        sessionRecipientId = sessionRole === 'recipient' ? (rows[0]?.id ?? null) : null
        sessionRecipientApproved = sessionRole === 'recipient' && rows[0]?.verification_status === 'approved'
      } else if (recipientResult) failures.push(`organizations: ${recipientResult.reason instanceof Error ? recipientResult.reason.message : 'request failed'}`)
      if (impactResult?.status === 'fulfilled') setImpact(impactResult.value)
      else if (impactResult) failures.push(`impact: ${impactResult.reason instanceof Error ? impactResult.reason.message : 'request failed'}`)
      if (notificationResult?.status === 'fulfilled') setNotifications(notificationResult.value.items)
      else if (notificationResult) failures.push(`notifications: ${notificationResult.reason instanceof Error ? notificationResult.reason.message : 'request failed'}`)
      setNotice(failures.length ? { kind: 'error', text: `Some information could not be loaded (${failures.join('; ')}). Use Refresh data to retry.` } : null)
      setLoading(false)
    })()
    refreshInFlight.current = task
    void task.finally(() => { if (refreshInFlight.current === task) refreshInFlight.current = null })
    return task
  }, [tab, user])
  useEffect(() => {
    if (!sessionToken) { setAuthReady(true); return }
    void api<User>('/api/auth/me').then(value => { sessionRole = value.role; setUser(value) }).catch(() => { sessionToken = null; localStorage.removeItem('annsetu-token') }).finally(() => setAuthReady(true))
  }, [])
  useEffect(() => { if (user) void refresh() }, [refresh, user])
  useEffect(() => {
    const requested = () => { void refresh(); setNotice({kind:'ok',text:'Donation request sent. It now appears under your active offers.'}) }
    const failed = (event:Event) => setNotice({kind:'error',text:(event as CustomEvent<string>).detail || 'Could not request this donation.'})
    window.addEventListener('annsetu:refresh', requested)
    window.addEventListener('annsetu:error', failed)
    return () => { window.removeEventListener('annsetu:refresh', requested); window.removeEventListener('annsetu:error', failed) }
  }, [refresh])
  async function perform(action: () => Promise<unknown>, message: string) {
    setBusy(true)
    try {
      const result = await action()
      await refresh()
      const matchingMessage = result && typeof result === 'object' && 'matching_message' in result && typeof result.matching_message === 'string' ? result.matching_message : null
      setNotice({ kind: 'ok', text: matchingMessage ?? message })
    }
    catch (e) { setNotice({ kind: 'error', text: e instanceof Error ? e.message : 'Something went wrong.' }) }
    finally { setBusy(false) }
  }
  async function confirmPortion(id: string, data: { step: 'pickup'|'receipt'; code: string }) {
    const updated = await api<Donation>(`/api/portions/${id}/confirm`, { method: 'POST', body: JSON.stringify(data) })
    setDonations(current => current.map(d => d.id === updated.id ? updated : d))
    if (data.step === 'receipt' && sessionRole === 'donor') {
      void api<Impact>('/api/impact').then(setImpact).catch(() => undefined)
    }
    return updated
  }
  async function submitDonation(e: FormEvent<HTMLFormElement>) {
    e.preventDefault(); if (user?.role !== 'donor') { setNotice({kind:'error',text:'Only donor accounts can publish food.'}); return } const form = e.currentTarget; const f = new FormData(form)
    const payload = Object.fromEntries(f.entries()) as Record<string, string>
    payload.quantity_kg = String(Number(payload.quantity_kg)); payload.latitude = String(Number(payload.latitude)); payload.longitude = String(Number(payload.longitude))
    payload.temperature_c = payload.temperature_c ? String(Number(payload.temperature_c)) : ''
    payload.prepared_at = new Date(payload.prepared_at).toISOString(); payload.expires_at = new Date(payload.expires_at).toISOString()
    await perform(async () => { const result = await api<Donation>('/api/donations', { method: 'POST', body: JSON.stringify({ ...payload, temperature_c: payload.temperature_c ? Number(payload.temperature_c) : null }) }); setTab('Donations'); return result }, 'Donation listed. Find eligible recipients when you’re ready.')
    form.reset()
  }
  async function submitRecipient(e: FormEvent<HTMLFormElement>) {
    e.preventDefault(); if (user?.role !== 'recipient') { setNotice({kind:'error',text:'Sign in with a recipient organization account to register a partner profile.'}); return } const form = e.currentTarget; const f = new FormData(form); const payload = Object.fromEntries(f.entries()) as Record<string, string>
    payload.latitude = String(Number(payload.latitude)); payload.longitude = String(Number(payload.longitude)); payload.capacity_kg = String(Number(payload.capacity_kg))
    let documentFile: File | null = f.get('document') instanceof File && (f.get('document') as File).size ? f.get('document') as File : null
    if (!documentFile) documentFile = await new Promise<File | null>(resolve => { const picker = window.document.createElement('input'); picker.type='file'; picker.accept='.pdf,.txt,application/pdf,text/plain'; picker.onchange=()=>resolve(picker.files?.[0] ?? null); picker.click() })
    if (!documentFile) { setNotice({kind:'error',text:'Choose a PDF or text registration document to submit.'}); return }
    await perform(async () => {
      const profile = await api<Recipient>('/api/recipients', { method: 'POST', body: JSON.stringify({ ...payload, categories: f.getAll('categories').map(String), storage: f.getAll('storage').map(String) }) })
      const upload = new FormData(); upload.set('file', documentFile as File); await api(`/api/recipients/${profile.id}/documents`, { method: 'POST', body: upload })
      return profile
    }, 'Organization registered. Its document is encrypted and the profile is waiting for admin review.')
    form.reset()
  }
  const nav: Tab[] = user?.role === 'admin' ? ['Overview','Donations','Recipients','Verification','Impact','Notifications'] : ['Overview','Donations','Recipients','Impact','Notifications']
  const pending = recipients.filter(r => r.verification_status === 'pending')
  const urgent = donations.filter(d => d.status === 'at_risk' || (d.status === 'open' && new Date(d.expires_at).getTime() - Date.now() < 4 * 3600000))

  if (!authReady) return <div className="auth-loading">Opening your AnnSetu workspace…</div>
  if (!user) return <AuthGate onAuth={(token,next)=>{sessionToken=token;sessionRole=next.role;localStorage.setItem('annsetu-token',token);setUser(next)}} />
  function logout() { sessionToken = null; localStorage.removeItem('annsetu-token'); setUser(null); setTab('Overview') }

  return <div className="shell" data-role={user.role} data-has-recipient={recipients.length ? 'true' : 'false'}>
    <aside className="sidebar">
      <a className="brand" href="#overview" onClick={() => setTab('Overview')}><span className="brand-mark">a.</span><span>annsetu<span className="brand-ai">ai</span><small>FOOD, FINDING ITS WAY</small></span></a>
      <div className="workspace-label">WORKSPACE</div>
      <nav>{nav.map((item, i) => <button key={item} className={`nav-item ${tab === item ? 'active' : ''}`} onClick={() => setTab(item)}><span className="nav-icon">{['⌂', '◈', '◎', '✓', '↗'][i]}</span>{(item === 'Overview' ? 'Dashboard' : item === 'Donations' ? (user?.role === 'recipient' ? 'Nearby food' : user?.role === 'admin' ? 'Food network' : 'Donations') : item === 'Recipients' ? (user?.role === 'recipient' ? 'Organization' : user?.role === 'admin' ? 'Organizations' : 'Nearby partners') : item)}{item === 'Verification' && pending.length > 0 && <span className="nav-badge">{pending.length}</span>}</button>)}</nav>
      <div className="sidebar-bottom"><div className="live-dot" /> API connection <span className="connection-state">{loading ? 'syncing' : notice?.kind === 'error' ? 'offline' : 'live'}</span><div className="sidebar-foot">Built for communities<br />across India <span>भारत के लिए बनाया</span></div></div>
    </aside>
    <main className="main">
      <button className="logout-button" onClick={logout}>Sign out · {user.name}</button>
      <header className="topbar"><div><span className="crumb">ANNSETU AI</span><span className="crumb-slash">/</span><span className="crumb-current">{tab}</span></div><div className="top-actions"><span className="date-chip">{new Date().toLocaleDateString([], { weekday: 'short', day: 'numeric', month: 'short', year: 'numeric' })}</span><button className="avatar" title="Workspace account">A</button></div></header>
      {notice && <div className={`notice ${notice.kind}`}><span>{notice.kind === 'ok' ? '✓' : '!'}</span>{notice.text}<button aria-label="Dismiss" onClick={() => setNotice(null)}>×</button></div>}

      {tab === 'Verification' && user.role === 'admin' && <AdminVerification recipients={pending} onRefresh={refresh} onError={message=>setNotice({kind:'error',text:message})} />}
      {user.role === 'recipient' && tab === 'Recipients' && recipients[0] && <RecipientWorkspace recipient={recipients[0]} onError={message=>setNotice({kind:'error',text:message})} />}
      {tab === 'Overview' && user.role === 'donor' && <>
        <section className="welcome"><div><div className="eyebrow"><span className="eyebrow-line" /> COMMUNITY FOOD NETWORK</div><h1>Good food has<br /><em>somewhere to go.</em></h1><p>Every meal saved is a little more hope, shared.</p><div className="welcome-actions"><button className="button primary" onClick={() => setTab('Donations')}>＋ List surplus food <span>→</span></button><button className="button quiet" onClick={() => setTab('Recipients')}>Browse recipients <span>↗</span></button></div></div><div className="hero-visual"><div className="sun-disc" /><div className="hero-leaf leaf-one">❧</div><div className="hero-leaf leaf-two">❧</div><div className="hero-bowl"><span>ðŸ²</span><i>made to be shared</i></div><div className="orbit orbit-one" /><div className="orbit orbit-two" /><div className="floating-note"><span>✳</span> One meal. A world of difference.</div></div></section>
        <div className="section-heading"><div><div className="eyebrow">THE NETWORK AT A GLANCE</div><h2>A little good, adding up.</h2></div><button className="text-button" onClick={() => setTab('Impact')}>See impact <span>→</span></button></div>
        <Stats impact={impact} hideRisk />
        <div className="overview-grid"><CompletedDeliveries donations={donations} />
          <section className="quote-card"><div className="quote-mark">?</div><p>When we share food, we share a future where nothing good goes to waste.</p><span>THE ANNSETU PROMISE</span><div className="quote-flower">?</div></section></div>
        <div className="section-heading recent-heading"><div><div className="eyebrow">FRESH FROM THE NETWORK</div><h2>Recently listed</h2></div><button className="text-button" onClick={() => setTab('Donations')}>All donations <span>→</span></button></div>
        {donations.filter(d => donationSection(d) === 'current' && (d.available_quantity_kg ?? Math.max(0, d.quantity_kg - (d.delivered_quantity_kg ?? 0) - (d.reserved_quantity_kg ?? 0))) > 0.01).length
          ? <DonationList donations={donations.filter(d => donationSection(d) === 'current' && (d.available_quantity_kg ?? Math.max(0, d.quantity_kg - (d.delivered_quantity_kg ?? 0) - (d.reserved_quantity_kg ?? 0))) > 0.01).slice(0, 3).map(remainingListingView)} busy={busy} onMatch={id => perform(() => api(`/api/donations/${id}/match`, { method: 'POST' }), 'Matching suggestions are ready.')} onAccept={id => perform(() => api(`/api/portions/${id}/accept`, { method: 'POST' }), 'Allocation accepted.')} onCancel={id => perform(() => api(`/api/portions/${id}/cancel`, { method: 'POST' }), 'Cancellation recorded and rescue search completed.')} onConfirm={(id, data) => perform(() => confirmPortion(id, data), `${data.step === 'pickup' ? 'Pickup' : 'Receipt'} confirmed.`)} deliveryCodes={deliveryCodes} setDeliveryCodes={setDeliveryCodes} />
          : <Empty title="No new listings" text="Recently delivered donations are shown in Completed deliveries and remain available under All donations." />}
      </>}

      {tab === 'Overview' && user.role === 'recipient' && <>
        <RecipientDashboard donations={donations} recipient={recipients[0]} />
        <div className="section-heading role-section-heading"><div><div className="eyebrow">WITHIN YOUR COLLECTION AREA</div><h2>Food available nearby</h2></div><button className="text-button" onClick={() => setTab('Donations')}>Browse all nearby food <span>→</span></button></div>
        <DonationList donations={donations.filter(d => ['open','at_risk','matched'].includes(d.status) && donationAvailableKg(d) > 0.01).slice(0, 4).map(remainingListingView)} busy={busy} onMatch={id => perform(() => api(`/api/donations/${id}/match`, { method: 'POST' }), 'Matching suggestions are ready.')} onAccept={id => perform(() => api(`/api/portions/${id}/accept`, { method: 'POST' }), 'Offer accepted.')} onCancel={id => perform(() => api(`/api/portions/${id}/cancel`, { method: 'POST' }), 'Offer cancelled.')} onConfirm={(id, data) => perform(() => confirmPortion(id, data), 'Receipt confirmed.')} deliveryCodes={deliveryCodes} setDeliveryCodes={setDeliveryCodes} />
      </>}

      {tab === 'Overview' && user.role === 'admin' && <>
        <AdminDashboard pendingCount={pending.length} urgentCount={urgent.length} onVerification={()=>setTab('Verification')} onImpact={()=>setTab('Impact')} />
        <div className="admin-overview-grid"><section className="panel admin-queue"><div className="panel-title"><div><div className="eyebrow">MODERATION QUEUE</div><h3>Organization applications</h3></div><span className={`count-pill ${pending.length ? 'warm' : ''}`}>{pending.length} waiting</span></div>{pending.length ? pending.slice(0,4).map(r=><div className="admin-queue-row" key={r.id}><span className="org-avatar org-2">{r.name.split(' ').map(x=>x[0]).slice(0,2).join('')}</span><div><strong>{r.name}</strong><small>{r.address}</small></div><button className="mini-button" onClick={()=>setTab('Verification')}>Review</button></div>) : <div className="empty-inline"><strong>Review queue is clear</strong><small>New organization applications will appear here.</small></div>}<button className="text-button admin-queue-link" onClick={()=>setTab('Verification')}>Open verification desk <span>→</span></button></section>
          <section className="panel admin-network"><div className="panel-title"><div><div className="eyebrow">NETWORK MONITOR</div><h3>Donation activity</h3></div><span className={`count-pill ${urgent.length ? 'warm' : ''}`}>{urgent.length} need attention</span></div><div className="admin-network-stats"><div><strong>{donations.length}</strong><span>Visible donations</span></div><div><strong>{donations.filter(d=>d.status==='completed').length}</strong><span>Completed</span></div><div><strong>{recipients.filter(r=>r.verification_status==='approved').length}</strong><span>Verified partners</span></div></div>{urgent.length ? <div className="admin-urgent-note"><b>{urgent.length} donation{urgent.length===1?'':'s'} need a closer look</b><span>Check expiry, allocation, and handover status.</span></div> : <div className="admin-urgent-note calm"><b>No urgent donations</b><span>The network is operating normally.</span></div>}<button className="text-button admin-queue-link" onClick={()=>setTab('Donations')}>Monitor donation records <span>→</span></button></section></div>
      </>}

      {tab === 'Donations' && <><PageTitle eyebrow="FROM SURPLUS TO SHARED" title={user.role === "recipient" ? "Food near your organization" : user.role === "admin" ? "Donation oversight" : "Your food donations"} subtitle={user.role === "recipient" ? "Browse safe donations and manage your nearby pickup requests." : user.role === "admin" ? "Review donation status and handover progress across the network." : "List surplus food and follow nearby matches through handover."} /><div className="content-grid"><section className="form-card"><div className="form-heading"><span className="form-icon">✳</span><div><h3>List surplus food</h3><p>A few details help us make a safe, timely match.</p></div></div><form className="field-grid" onSubmit={submitDonation}><label className="field span-two">Donor or kitchen name<input name="donor_name" placeholder="e.g. Green Leaf Kitchen" minLength={2} required /></label><label className="field">Food name<input name="food_name" placeholder="e.g. Vegetable pulao" required /></label><label className="field">Food type<select name="category"><option value="veg">Vegetarian</option><option value="non-veg">Non-vegetarian</option></select></label><label className="field">Quantity (kg)<input name="quantity_kg" type="number" min="0.1" step="0.1" placeholder="12" required /></label><label className="field">Storage condition<select name="storage"><option value="hot">Hot-held</option><option value="ambient">Room temperature</option><option value="chilled">Chilled</option></select></label><label className="field">Prepared at<input name="prepared_at" type="datetime-local" defaultValue={localDateTime(new Date())} required /></label><label className="field">Consume by<input name="expires_at" type="datetime-local" defaultValue={localDateTime(new Date(Date.now() + 3 * 3600000))} required /></label><label className="field">Temperature (°C)<input name="temperature_c" type="number" step="0.1" placeholder="e.g. 63" required /></label><label className="field">Pickup address<input name="pickup_address" placeholder="Street, area, city" required /></label><label className="field">Latitude<input name="latitude" type="number" step="any" placeholder="28.6139" required /></label><label className="field">Longitude<input name="longitude" type="number" step="any" placeholder="77.2090" required /></label><div className="location-helper span-two"><LocationAutofill/><span>Use the pickup point for accurate nearby matching. Enter its street address above.</span></div><label className="field span-two">Handling notes<textarea name="notes" rows={3} placeholder="Allergens, packaging, or pickup instructions" /></label><div className="form-foot span-two"><span>✦ Your details help keep every handover safe.</span><button className="button primary" disabled={busy}>Publish donation <span>→</span></button></div></form></section><aside className="tip-card"><span className="tip-spark">✳</span><div className="eyebrow">A QUICK SAFETY NOTE</div><h3>Freshness first,<br />always.</h3><p>Share the actual preparation time and a realistic consume-by deadline. Only organizations with matching storage and food-type requirements will be suggested.</p><div className="safety-rule"><span>60°C</span><small>minimum hot-holding temperature</small></div><div className="safety-rule"><span>5°C</span><small>maximum chilled storage temperature</small></div><small className="disclaimer">Prototype thresholds are conservative checks, not a substitute for current FSSAI guidance.</small></aside></div><DonationSections donations={donations} busy={busy} onMatch={id => perform(() => api(`/api/donations/${id}/match`, { method: 'POST' }), 'Matching suggestions are ready.')} onAccept={id => perform(() => api(`/api/portions/${id}/accept`, { method: 'POST' }), 'Allocation accepted.')} onCancel={id => perform(() => api(`/api/portions/${id}/cancel`, { method: 'POST' }), 'Cancellation recorded and rescue search completed.')} onConfirm={(id, data) => perform(() => confirmPortion(id, data), `${data.step === 'pickup' ? 'Pickup' : 'Receipt'} confirmed.`)} deliveryCodes={deliveryCodes} setDeliveryCodes={setDeliveryCodes} /></>}

      {tab === 'Recipients' && <><PageTitle eyebrow="GOOD PEOPLE, GOOD FOOD" title={user.role === "donor" ? "Partners near your donations" : user.role === "admin" ? "Partner directory" : "Your organization profile"} subtitle={user.role === "donor" ? "Approved organizations within 35 km of your listed donation locations." : user.role === "admin" ? "Review verified partners and organization details across the network." : "Manage your verification status and registration documents."} /><div className="recipient-grid">{recipients.filter(r => r.verification_status === 'approved').map((r, i) => <article className="recipient-card" key={r.id}><div className={`org-avatar org-${i % 4}`}>{r.name.split(' ').map(x => x[0]).slice(0, 2).join('')}</div><span className="verified-tag">✓ VERIFIED</span><h3>{r.name}</h3><p className="org-location">⌖ {r.address}</p><div className="org-facts"><div><small>CAPACITY</small><strong>{r.capacity_kg} kg</strong></div><div><small>FOOD TYPES</small><strong>{r.categories.map(c => c === 'veg' ? 'Veg' : 'Non-veg').join(' · ')}</strong></div></div><div className="org-storage">Storage: {r.storage.join(', ')}</div><div className="org-contact">{r.contact}<span>{r.hours}</span></div></article>)}{recipients.filter(r => r.verification_status === 'approved').length === 0 && <Empty title={user.role === "donor" ? "No nearby partners yet" : "No approved partners yet"} text={user.role === "donor" ? (donations.length ? "No approved organization is within 35 km of your donation locations." : "List a donation with its pickup location to find nearby organizations.") : "Organizations can apply below. Approved partners appear here."} />}</div><section className="form-card recipient-form"><div className="form-heading"><span className="form-icon lavender">◎</span><div><h3>Join the network</h3><p>Register your organization for admin review.</p></div></div><form className="field-grid" onSubmit={submitRecipient}><label className="field">Organization name<input name="name" required /></label><label className="field">Contact number<input name="contact" type="tel" required /></label><label className="field span-two">Full address<input name="address" required /></label><label className="field">Latitude<input name="latitude" type="number" step="any" placeholder="28.6139" required /></label><label className="field">Longitude<input name="longitude" type="number" step="any" placeholder="77.2090" required /></label><div className="location-helper span-two"><LocationAutofill/><span>Use your organization?s collection location. Enter its street address above.</span></div><label className="field">Daily capacity (kg)<input name="capacity_kg" type="number" min="1" required /></label><label className="field">Collection hours<input name="hours" placeholder="Daily, 9 am–8 pm" required /></label><fieldset className="field span-two check-field"><legend>Food types accepted</legend><label><input type="checkbox" name="categories" value="veg" defaultChecked /> Vegetarian</label><label><input type="checkbox" name="categories" value="non-veg" /> Non-vegetarian</label></fieldset><fieldset className="field span-two check-field"><legend>Storage available</legend><label><input type="checkbox" name="storage" value="hot" defaultChecked /> Hot holding</label><label><input type="checkbox" name="storage" value="ambient" defaultChecked /> Ambient</label><label><input type="checkbox" name="storage" value="chilled" /> Chilled</label></fieldset><label className="field">Registration reference<input name="reference_id" placeholder="Optional Darpan ID" /></label><label className="field">Evidence summary<textarea name="evidence" minLength={10} placeholder="Document type and details submitted for review" required /></label><div className="form-foot span-two"><span>Private documents should be shared through a secure channel.</span><button className="button primary" disabled={busy}>Submit for review <span>→</span></button></div></form></section></>}

      {tab === 'Verification' && <><PageTitle eyebrow="TRUST, BUILT TOGETHER" title="Verification desk" subtitle="Evidence is reviewed by a person. Official records are not automatically verified in this prototype." /><div className="review-banner"><span>ⓘ</span><p><strong>Evidence-based, human-reviewed.</strong> A registration ID or document is supporting evidence. Approval records an admin decision; it does not mean government endorsement.</p></div><div className="review-list">{pending.map(r => <article className="review-card" key={r.id}><div className="review-top"><div className="org-avatar org-2">{r.name.split(' ').map(x => x[0]).slice(0,2).join('')}</div><div className="grow"><h3>{r.name}</h3><small>{r.address} · Submitted {dateLabel(r.created_at)}</small></div><span className="status pending">PENDING REVIEW</span></div><div className="evidence-box"><small>SUBMITTED EVIDENCE</small><p>{r.evidence}</p>{r.reference_id && <span>Reference: {r.reference_id}</span>}<div className="verification-report"><b>Document details</b><span>{r.verification_report.document_details}</span><b>Registration reference</b><span>{r.verification_report.registration_reference}</span><b>Official records</b><span>{r.verification_report.official_record_check}</span><b>Next step</b><span>{r.verification_report.recommendation}</span></div></div><div className="review-bottom"><span>Capacity {r.capacity_kg} kg · {r.categories.join(', ')} · {r.storage.join(', ')}</span><div><button disabled={busy} className="button reject" onClick={() => void perform(() => api(`/api/admin/recipients/${r.id}/review`, { method: 'POST', body: JSON.stringify({ decision: 'rejected', note: 'Documents need clarification.' }) }), 'Application rejected.')}>Reject application</button><button disabled={busy} className="button approve" onClick={() => void perform(() => api(`/api/admin/recipients/${r.id}/review`, { method: 'POST', body: JSON.stringify({ decision: 'approved', note: 'Reviewed by administrator.' }) }), 'Organization approved for matching.')}>✓ Approve partner</button></div></div></article>)}{pending.length === 0 && <Empty title="You’re all caught up" text="New organization applications will appear here for review." />}</div></>}

      {tab === 'Impact' && <><PageTitle eyebrow="SMALL ACTS, BIG RIPPLE" title="The good we do together." subtitle="A transparent look at the food reaching community tables." /><Stats impact={impact} large /><div className="impact-explainer"><span className="impact-flower">✿</span><div><div className="eyebrow">HOW WE COUNT</div><h3>Real handovers. Honest numbers.</h3><p>Redistributed quantity includes portions confirmed received by the recipient. Estimated meals use a 400 g serving size. Waste avoided equals confirmed food quantity; CO₂e uses an illustrative 2.5 kg per kg estimate. These are directional impact estimates, not audited environmental claims.</p></div></div><div className="impact-footnote">✦ Food rescued updates after pickup confirmation; meals shared updates after recipient receipt confirmation.</div></>}
      {tab === 'Notifications' && <><PageTitle eyebrow="STAY IN THE LOOP" title="Notifications" subtitle="Updates about offers, handovers, and organization review." /><div className="notice-list">{notifications.map(n=><article className={`notice-card ${n.read_at?'read':''}`} key={n.id}><div><b>{n.title}</b><p>{n.body}</p><small>{dateLabel(n.created_at)}</small></div>{!n.read_at&&<button className="mini-button" onClick={()=>void perform(()=>api(`/api/notifications/${n.id}/read`,{method:'POST'}),'Notification marked read.')}>Mark read</button>}</article>)}{notifications.length===0&&<Empty title="All caught up" text="Important updates will appear here." />}</div></>}
      <footer className="footer"><span>✳ <strong>annsetu ai</strong></span><span>Good food travels further, together.</span><button onClick={() => void refresh()}>↻ Refresh data</button></footer>
    </main>
  </div>
}

function LocationAutofill() {
  const [busy, setBusy] = useState(false)
  const [message, setMessage] = useState('')
  function locate(event: MouseEvent<HTMLButtonElement>) {
    const form = event.currentTarget.closest('form')
    if (!navigator.geolocation) { setMessage('Location is not supported by this browser.'); return }
    setBusy(true); setMessage('Finding your current location…')
    navigator.geolocation.getCurrentPosition(position => {
      const values = { latitude: position.coords.latitude.toFixed(6), longitude: position.coords.longitude.toFixed(6) }
      for (const [name, value] of Object.entries(values)) {
        const input = form?.elements.namedItem(name)
        if (input instanceof HTMLInputElement) {
          const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')?.set
          setter?.call(input, value)
          input.dispatchEvent(new Event('input', { bubbles: true }))
          input.dispatchEvent(new Event('change', { bubbles: true }))
        }
      }
      setMessage(`Coordinates filled · accuracy about ${Math.round(position.coords.accuracy)} m`)
      setBusy(false)
    }, error => {
      const reason = error.code === error.PERMISSION_DENIED ? 'Allow location access in your browser to continue.' : error.code === error.POSITION_UNAVAILABLE ? 'Your current location could not be determined.' : 'Location lookup timed out. Please try again.'
      setMessage(reason); setBusy(false)
    }, { enableHighAccuracy: false, timeout: 15000, maximumAge: 60000 })
  }
  return <div className="location-autofill"><button type="button" className="mini-button" onClick={locate} disabled={busy}>{busy ? 'Locating…' : '⌖ Use my current location'}</button><span aria-live="polite">{message}</span></div>
}

function AuthGate({ onAuth }: { onAuth: (token: string, user: User) => void }) {
  const [mode, setMode] = useState<'login'|'register'>('register')
  const [role, setRole] = useState<'donor'|'recipient'>('donor')
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  async function submit(e: FormEvent<HTMLFormElement>) {
    e.preventDefault(); setError(''); setBusy(true)
    const values = Object.fromEntries(new FormData(e.currentTarget).entries())
    try {
      const result = await api<{access_token:string;user:User}>(`/api/auth/${mode === 'register' ? 'register' : 'login'}`, { method:'POST', body:JSON.stringify(mode === 'register' ? {...values,role} : values) })
      onAuth(result.access_token,result.user)
    } catch (err) { setError(err instanceof Error ? err.message : 'Could not sign in.') }
    finally { setBusy(false) }
  }
  return <main className="auth-screen"><section className="auth-card"><div className="brand-mark">a.</div><div className="eyebrow">FOOD, FINDING ITS WAY</div><h1>{mode === 'register' ? 'Join AnnSetu AI' : 'Welcome back'}</h1><p>Connect surplus food with community organizations.</p><form onSubmit={submit} className="auth-form">{mode === 'register' && <><label className="field">Your name<input name="name" minLength={2} required /></label><label className="field">I am a<select value={role} onChange={e=>setRole(e.target.value as 'donor'|'recipient')}><option value="donor">Food donor</option><option value="recipient">Recipient organization</option></select></label></>}<label className="field">Email<input name="email" type="email" required /></label><label className="field">Password<input name="password" type="password" minLength={mode==='register'?10:1} required /></label>{error && <p className="auth-error">{error}</p>}<button className="button primary" disabled={busy}>{busy?'Please wait…':mode==='register'?'Create account':'Sign in'}</button></form><button className="text-button auth-switch" onClick={()=>{setMode(mode==='register'?'login':'register');setError('')}}>{mode==='register'?'Already have an account? Sign in':'Create a donor or recipient account'}</button><small>Administrators sign in with credentials configured by the service operator.</small></section></main>
}
function RecipientDashboard({ donations, recipient }: { donations:Donation[]; recipient?:Recipient }) {
  const assigned = donations.filter(d=>d.portions.some(p=>p.recipient_id===recipient?.id))
  const offered = assigned.flatMap(d=>d.portions).filter(p=>p.recipient_id===recipient?.id&&p.status==='offered').length
  const inTransit = assigned.flatMap(d=>d.portions).filter(p=>p.recipient_id===recipient?.id&&['accepted','picked_up'].includes(p.status)).length
  return <section className="role-dashboard recipient-dashboard"><div><div className="eyebrow">RECIPIENT WORKSPACE</div><h2>{recipient ? `Welcome, ${recipient.name}` : 'Complete your organization profile'}</h2><p>{recipient ? `Profile status: ${statusLabel(recipient.verification_status)}. Track your offers and handovers here.` : 'Create an organization profile in Community partners to start receiving suitable donations.'}</p></div><div className="role-metrics"><article><b>{offered}</b><span>Offers to review</span></article><article><b>{inTransit}</b><span>Active handovers</span></article><article><b>{donations.filter(d=>['open','at_risk'].includes(d.status)).length}</b><span>Food available</span></article></div></section>
}

function AdminDashboard({ pendingCount, urgentCount, onVerification, onImpact }: { pendingCount:number; urgentCount:number; onVerification:()=>void; onImpact:()=>void }) {
  return <section className="role-dashboard admin-dashboard"><div><div className="eyebrow">ADMIN OPERATIONS</div><h2>Network oversight</h2><p>Review incoming organizations, monitor at-risk food, and track verified impact.</p></div><div className="admin-shortcuts"><button onClick={onVerification}><strong>{pendingCount}</strong><span>Applications waiting</span><em>Open review desk →</em></button><button onClick={onImpact}><strong>{urgentCount}</strong><span>Donations needing attention</span><em>View network impact →</em></button></div></section>
}

function RecipientWorkspace({ recipient, onError }: { recipient:Recipient; onError:(message:string)=>void }) {
  const [documents,setDocuments]=useState<PrivateDocument[]>([])
  useEffect(()=>{ void api<PrivateDocument[]>(`/api/recipients/${recipient.id}/documents`).then(setDocuments).catch(e=>onError(e instanceof Error?e.message:'Could not load private documents.')) },[recipient.id])
  async function download(doc:PrivateDocument) { try { const response=await fetch(`${API}/api/recipients/${recipient.id}/documents/${doc.id}`,{headers:{Authorization:`Bearer ${sessionToken}`}}); if(!response.ok) throw new Error('Document download failed'); const link=window.document.createElement('a'); link.href=URL.createObjectURL(await response.blob()); link.download=doc.filename; link.click(); URL.revokeObjectURL(link.href) } catch(e) { onError(e instanceof Error?e.message:'Document download failed.') } }
  async function remove(doc:PrivateDocument) { try { await api(`/api/recipients/${recipient.id}/documents/${doc.id}`,{method:'DELETE'}); setDocuments(old=>old.filter(item=>item.id!==doc.id)) } catch(e) { onError(e instanceof Error?e.message:'Document removal failed.') } }
  return <section className="recipient-workspace"><div><div className="eyebrow">YOUR ORGANIZATION</div><h3>{recipient.name}</h3><span className={`status ${recipient.verification_status}`}>{statusLabel(recipient.verification_status)}</span></div><p>Verification is reviewed by an administrator. Official record checks may remain unconfirmed.</p><div>{documents.map(doc=><article key={doc.id}><span>{doc.filename}</span><button className="text-button" onClick={()=>void download(doc)}>Download</button>{recipient.verification_status==='pending'&&<button className="text-button" onClick={()=>void remove(doc)}>Remove</button>}</article>)}{documents.length===0&&<small>No private registration document uploaded.</small>}</div></section>
}

function AdminVerification({ recipients, onRefresh, onError }: { recipients:Recipient[]; onRefresh:()=>Promise<void>; onError:(message:string)=>void }) {
  const [reports,setReports]=useState<Record<string,Recipient['verification_report']>>({})
  const [files,setFiles]=useState<Record<string,PrivateDocument[]>>({})
  const [busy,setBusy]=useState('')
  async function analyze(id:string) { setBusy(id); try { const [result,docs]=await Promise.all([api<{report:Recipient['verification_report']}>(`/api/admin/recipients/${id}/verify`,{method:'POST'}),api<PrivateDocument[]>(`/api/admin/recipients/${id}/documents`)]); setReports(old=>({...old,[id]:result.report})); setFiles(old=>({...old,[id]:docs})); await onRefresh() } catch(e) { onError(e instanceof Error?e.message:'Could not review documents.') } finally { setBusy('') } }
  async function download(recipientId:string, doc:PrivateDocument) { try { const response=await fetch(`${API}/api/recipients/${recipientId}/documents/${doc.id}`,{headers:{Authorization:`Bearer ${sessionToken}`}}); if(!response.ok) throw new Error('Document download failed'); const link=window.document.createElement('a'); link.href=URL.createObjectURL(await response.blob()); link.download=doc.filename; link.click(); URL.revokeObjectURL(link.href) } catch(e) { onError(e instanceof Error?e.message:'Document download failed.') } }
  return <section className="admin-verification"><div><b>Document and registry checks</b><p>Run the evidence workflow before deciding. Every approval remains an administrator decision.</p></div>{recipients.map(r=><article key={r.id}><div><strong>{r.name}</strong><span>{r.reference_id||'No registration reference'}</span></div><button className="mini-button" disabled={busy===r.id} onClick={()=>void analyze(r.id)}>{busy===r.id?'Checking…':'Analyze documents'}</button>{(reports[r.id]||r.verification_report)?.checks?.map((c,i)=><p key={i}><b>{c.agent}: {c.status}</b> — {c.detail}</p>)}{(reports[r.id]||r.verification_report)?.flags?.map((f,i)=><p className="auth-error" key={i}>{f}</p>)}{files[r.id]?.map(d=><button className="text-button" key={d.id} onClick={()=>void download(r.id,d)}>Download {d.filename}</button>)}</article>)}
  </section>
}

function localDateTime(date: Date) { const d = new Date(date.getTime() - date.getTimezoneOffset() * 60000); return d.toISOString().slice(0, 16) }
function PageTitle({ eyebrow, title, subtitle }: { eyebrow: string; title: string; subtitle: string }) { return <section className="page-title"><div className="eyebrow">{eyebrow}</div><h1>{title}</h1><p>{subtitle}</p></section> }
function CompletedDeliveries({ donations }: { donations: Donation[] }) {
  const deliveries = donations.flatMap(donation => donation.portions
    .filter(portion => portion.status === 'received')
    .map(portion => ({ donation, portion })))
    .sort((a, b) => new Date(b.portion.received_at ?? 0).getTime() - new Date(a.portion.received_at ?? 0).getTime())
  return <section className="panel completed-deliveries"><div className="panel-title"><div><div className="eyebrow">HANDOVERS FINISHED</div><h3>Completed deliveries</h3></div><span className="count-pill">{deliveries.length}</span></div>{deliveries.length ? deliveries.slice(0, 4).map(({ donation, portion }) => <div className="completed-delivery-row" key={portion.id}><span className="food-dot">?</span><div className="grow"><strong>{donation.food_name}</strong><small>{portion.recipient_name} ? {portion.quantity_kg} kg</small></div><time>{portion.received_at ? dateLabel(portion.received_at) : 'Received'}</time></div>) : <div className="empty-inline"><strong>No completed deliveries yet</strong><small>Deliveries appear here after the recipient confirms receipt.</small></div>}</section>
}
function Stats({ impact, large = false, hideRisk = false }: { impact: Impact | null; large?: boolean; hideRisk?: boolean }) {
  const items = [{ label: 'MEALS SHARED', value: impact?.meals_redistributed ?? '—', icon: '✿', tone: 'green', sub: 'Confirmed received' }, { label: 'FOOD RESCUED', value: `${impact?.quantity_redistributed_kg ?? '—'} kg`, icon: '✳', tone: 'peach', sub: 'Pickup confirmed or received' }, { label: 'SUCCESSFUL RESCUES', value: impact?.successful_rescues ?? '—', icon: '↗', tone: 'lavender', sub: 'Donations with confirmed delivery' }, { label: 'NEEDS A HAND', value: impact?.at_risk_donations ?? '—', icon: '♡', tone: 'yellow', sub: 'Open rescue watch' }]
  return <div className={`stats-grid ${large ? 'stats-large' : ''} ${hideRisk ? 'stats-no-risk' : ''}`}>{items.filter(s => !hideRisk || s.label !== 'NEEDS A HAND').map(s => <article className="stat-card" key={s.label}><div className={`stat-icon ${s.tone}`}>{s.icon}</div><div className="stat-label">{s.label}</div><div className="stat-value">{s.value}</div><div className="stat-sub">{s.sub}</div></article>)}</div>
}
function DonationList({ donations, busy, onMatch, onAccept, onCancel, onConfirm, deliveryCodes, setDeliveryCodes }: { donations: Donation[]; busy: boolean; onMatch: (id: string) => void; onAccept: (id: string) => void; onCancel: (id: string) => void; onConfirm: (id: string, data: { step: 'pickup'|'receipt'; code: string }) => void; deliveryCodes: Record<string,string>; setDeliveryCodes: (v: Record<string,string>) => void }) {
  const [requestQuantities, setRequestQuantities] = useState<Record<string,string>>({})
  async function requestDonation(id:string, quantity:number) { try { await api(`/api/donations/${id}/request`,{method:'POST',body:JSON.stringify({quantity_kg:quantity})}); window.dispatchEvent(new Event('annsetu:refresh')) } catch(e) { window.dispatchEvent(new CustomEvent('annsetu:error',{detail:e instanceof Error?e.message:'Could not request this donation.'})) } }
  if (!donations.length) return <Empty title={sessionRole === "recipient" ? "No nearby food right now" : sessionRole === "admin" ? "No donation records yet" : "No donations listed yet"} text={sessionRole === "recipient" ? "New safe donations within your collection area will appear here." : sessionRole === "admin" ? "Donation activity will appear here when food is listed." : "List surplus food to connect with nearby community partners."} />
  return <div className="donation-list">{donations.map(d => { const displayStatus = donationDisplayStatus(d); const deliveredKg = donationDeliveredKg(d); const remainingKg = d.current_listing_quantity_kg ?? donationRemainingKg(d); const availableKg = d.current_listing_expired ? 0 : d.available_quantity_kg ?? Math.max(0, remainingKg - (d.reserved_quantity_kg ?? 0)); return <article className="donation-card" key={d.id}><div className="donation-main"><div className="food-illustration">{d.category === 'veg' ? '🥗' : 'ðŸ±'}</div><div className="donation-info"><div className="donation-kicker">{d.id} <span>·</span> {d.donor_name}</div><h3>{d.food_name}</h3><p>{d.current_listing_view ? `${remainingKg} kg remaining; ${availableKg} kg available; ${d.quantity_kg} kg originally listed` : `${d.quantity_kg} kg total`} · {d.category === 'veg' ? 'Vegetarian' : 'Non-vegetarian'} · {d.storage}</p><div className="donation-meta"><span>⌖ {d.pickup_address}</span><span>◷ Consume by {dateLabel(d.expires_at)}</span>{!d.current_listing_view && deliveredKg > 0 && <span>{deliveredKg} kg delivered</span>}{(d.reserved_quantity_kg ?? 0) > 0 && <span>{d.reserved_quantity_kg} kg reserved</span>}{!d.current_listing_view && availableKg > 0 && <span>{availableKg} kg available</span>}</div></div><span className={`status ${displayStatus.key}`}>{displayStatus.label}</span></div>
    {d.portions?.length > 0 && <div className="allocation-list"><div className="allocation-label">RECIPIENT ALLOCATIONS</div>{d.portions.map(p => <div className="allocation" key={p.id}><div className="allocation-org"><span className="tiny-org">◎</span><span><strong>{p.recipient_name}</strong><small>{p.quantity_kg} kg{p.distance_km ? ` · ${p.distance_km} km away` : ''}</small></span></div><span className={`status ${p.status}`}>{statusLabel(p.status)}</span><div className="allocation-actions">
      {p.status === 'offered' && <>{sessionRole === 'recipient' && p.recipient_id === sessionRecipientId && <button className="mini-button" disabled={busy} onClick={() => onAccept(p.id)}>Accept offer</button>}{(sessionRole !== 'recipient' || p.recipient_id === sessionRecipientId) && <button className="mini-button subtle" disabled={busy} onClick={() => onCancel(p.id)}>Can’t collect</button>}</>}
      {p.status === 'accepted' && sessionRole === 'recipient' && p.recipient_id === sessionRecipientId && <DeliveryCode donationId={d.id} />}
      {p.status === 'accepted' && sessionRole === 'donor' && <ConfirmAction label="Confirm pickup" portion={p} deliveryCodes={deliveryCodes} setDeliveryCodes={setDeliveryCodes} busy={busy} onConfirm={onConfirm} />}
      {p.status === 'picked_up' && ['recipient','admin'].includes(sessionRole) && (sessionRole !== 'recipient' || p.recipient_id === sessionRecipientId) && <ConfirmAction label="Confirm receipt" portion={p} deliveryCodes={deliveryCodes} setDeliveryCodes={setDeliveryCodes} busy={busy} onConfirm={onConfirm} />}
    </div>{['accepted','picked_up','received'].includes(p.status) && <HandoverProgress portion={p} events={d.timeline} />}<DeliveryMap donation={d} portion={p} /></div>)}</div>}
    {sessionRole === 'recipient' && donationSection(d) === 'current' && ['open','at_risk','matched'].includes(d.status) && donationAvailableKg(d) > 0.01 && !d.portions.some(p=>p.recipient_id===sessionRecipientId) && <div className={`donation-actions recipient-request ${d.request_eligibility?.eligible ? 'request-eligible' : 'request-blocked'}`}>
      {d.request_eligibility?.eligible ? <><span>{d.request_eligibility.distance_km} km away · {d.available_quantity_kg ?? '—'} kg available</span><input aria-label={`Requested quantity for ${d.food_name}`} className="request-quantity" type="number" min="0.1" max={d.available_quantity_kg} step="0.1" placeholder={`Up to ${d.available_quantity_kg} kg`} value={requestQuantities[d.id] ?? ''} onChange={e=>setRequestQuantities(current=>({...current,[d.id]:e.target.value}))} /><button className="button primary small" disabled={busy || !sessionRecipientApproved || !(Number(requestQuantities[d.id]) > 0) || Number(requestQuantities[d.id]) > (d.available_quantity_kg ?? 0)} onClick={()=>void requestDonation(d.id,Number(requestQuantities[d.id]))}>Request quantity <span>→</span></button></> : <><span className="eligibility-label">Why you can’t request this yet</span><span className="eligibility-reason">{d.request_eligibility?.reason ?? (sessionRecipientApproved ? 'Checking collection requirements…' : 'Your organization must be approved to request food.')}</span></>}
    </div>}
    {['open','at_risk','matched'].includes(d.status) && donationSection(d) === 'current' && donationAvailableKg(d) > 0.01 && ['donor','admin'].includes(sessionRole) && <div className={`donation-actions ${d.status === 'at_risk' ? 'rescue-actions' : ''}`}><span>{donationDeliveredKg(d) > 0 ? `${donationAvailableKg(d)} kg remains available for matching.` : d.status === 'at_risk' ? `At risk · ${donationAvailableKg(d)} kg needs a recipient.` : 'Ready to find an eligible community partner?'}</span><button className="button primary small" disabled={busy} onClick={() => onMatch(d.id)}>Find matches <span>→</span></button></div>}
    {d.timeline?.length > 0 && <details className="timeline"><summary>Donation timeline <span>{d.timeline.length} updates</span></summary><div>{d.timeline.slice(0,5).map((e,i)=><p key={`${e.created_at}-${i}`}><i />{e.detail}<small>{dateLabel(e.created_at)}</small></p>)}</div></details>}
  </article>})}</div>
}
function DonationSections(props: Parameters<typeof DonationList>[0]) {
  const sections = [
    { key: 'current' as const, title: 'Current listings', empty: 'No current listings. New donations you publish will appear here.', rows: props.donations.filter(d => donationSection(d) === 'current') },
    { key: 'completed' as const, title: 'Completed deliveries', empty: 'Receipt-confirmed portions appear here, including partial deliveries.', rows: props.donations.filter(d => d.portions?.some(p => p.status === 'received')) },
    { key: 'expired' as const, title: 'Expired donations', empty: 'Donations past their consume-by deadline appear here for history.', rows: props.donations.filter(d => donationSection(d) === 'expired') },
  ]
  return <div className="donation-sections">{sections.map(section => <section className="donation-section" key={section.key}><div className="section-heading list-heading"><div><div className="eyebrow">{section.key === 'current' ? 'AVAILABLE OR IN HANDOVER' : section.key === 'completed' ? 'RECEIPT CONFIRMED' : 'CONSUME-BY PASSED'}</div><h2>{section.title}</h2></div><span className="count-pill">{section.key === 'completed' ? section.rows.reduce((count,d)=>count+d.portions.filter(p=>p.status==='received').length,0) : section.rows.length}</span></div>{section.rows.length ? section.key === 'completed' ? <CompletedPortions donations={section.rows} /> : <DonationList {...props} donations={section.rows.map(remainingListingView)} /> : <div className="empty-inline"><strong>{section.title} is empty</strong><small>{section.empty}</small></div>}</section>)}</div>
}
function CompletedPortions({ donations }: { donations: Donation[] }) {
  const deliveries = donations.flatMap(d => d.portions.filter(p => p.status === 'received').map(portion => ({ donation: d, portion })))
    .sort((a,b)=>new Date(b.portion.received_at ?? 0).getTime()-new Date(a.portion.received_at ?? 0).getTime())
  return <div className="donation-list">{deliveries.map(({donation,portion})=><article className="donation-card completed-portion" key={portion.id}><div className="donation-main"><div className="food-illustration">✓</div><div className="donation-info"><div className="donation-kicker">{donation.id} <span>·</span> {donation.donor_name}</div><h3>{donation.food_name}</h3><p>{portion.quantity_kg} kg delivered · {donation.quantity_kg} kg originally listed</p><div className="donation-meta"><span>Recipient · {portion.recipient_name}</span><span>Received · {portion.received_at ? dateLabel(portion.received_at) : 'Confirmed'}</span></div></div><span className="status received">received</span></div><HandoverProgress portion={portion} events={donation.timeline} /><DeliveryMap donation={donation} portion={portion} /></article>)}</div>
}
function HandoverProgress({ portion, events }: { portion: Portion; events: Donation['timeline'] }) {
  const steps = [
    { type: 'accepted', label: 'Accepted', fallback: portion.status === 'accepted' || portion.status === 'picked_up' || portion.status === 'received' },
    { type: 'picked_up', label: 'Picked up', fallback: portion.status === 'picked_up' || portion.status === 'received' },
    { type: 'received', label: 'Receipt confirmed', fallback: portion.status === 'received' },
  ]
  return <div className="handover-progress" aria-label="Handover history">{steps.map(step => {
    const entry = events.find(event => event.portion_id === portion.id && event.event_type === step.type)
    const timestamp = entry?.created_at || (step.type === 'picked_up' ? portion.pickup_at : step.type === 'received' ? portion.received_at : null)
    const label = step.type === 'received' && !step.fallback ? 'Receipt' : step.label
    return <div className={`handover-progress-step ${entry || step.fallback ? 'complete' : ''}`} key={step.type}><strong>{label}</strong><small>{timestamp ? dateLabel(timestamp) : step.fallback ? 'Time not recorded' : 'Pending'}</small></div>
  })}</div>
}
function DeliveryCode({ donationId }: { donationId: string }) {
  const [code,setCode]=useState('')
  const [error,setError]=useState('')
  const [loading,setLoading]=useState(false)
  async function reveal() { setLoading(true); setError(''); try { const result=await api<{code:string}>(`/api/donations/${donationId}/delivery-code`); setCode(result.code) } catch(e) { setError(e instanceof Error?e.message:'Delivery code could not be loaded.') } finally { setLoading(false) } }
  useEffect(()=>{void reveal()},[donationId])
  return <div className="delivery-code"><span>DELIVERY CODE</span>{loading&&!code&&<small role="status">Loading code...</small>}{code&&<strong aria-label="Delivery code">{code}</strong>}{error&&<><small role="alert">{error}</small><button type="button" className="mini-button" disabled={loading} onClick={()=>void reveal()}>Retry</button></>}</div>
}
function ConfirmAction({ label, portion, deliveryCodes, setDeliveryCodes, busy, onConfirm }: { label: string; portion: Portion; deliveryCodes: Record<string,string>; setDeliveryCodes: (v: Record<string,string>) => void; busy: boolean; onConfirm: (id:string,data:{step:'pickup'|'receipt';code:string})=>void }) {
  const currentStep = portion.status === 'picked_up' ? 'receipt' : 'pickup'
  if (currentStep === 'receipt') return <button className="mini-button" disabled={busy} onClick={() => onConfirm(portion.id,{step:'receipt',code:''})}>{label}</button>
  return <form className="confirm-form" onSubmit={e => { e.preventDefault(); onConfirm(portion.id,{ step: currentStep, code: deliveryCodes[portion.id] ?? '' }) }}><label className="pickup-code-prompt">Enter the one-time code shown in the NGO listing<input aria-label="NGO delivery code" inputMode="text" maxLength={16} placeholder="Enter 16-character code" value={deliveryCodes[portion.id] ?? ''} onChange={e => setDeliveryCodes({ ...deliveryCodes, [portion.id]: e.target.value.toUpperCase().replace(/[^A-F0-9]/g,'').slice(0,16) })} /></label><button className="mini-button" disabled={busy || (deliveryCodes[portion.id] ?? '').length !== 16}>{label}</button></form>
}
const ROUTING_API = (import.meta.env.VITE_OSRM_BASE_URL ?? 'https://router.project-osrm.org').replace(/\/$/, '')
const pickupIcon = L.divIcon({ className: 'delivery-marker pickup-marker', html: '<span>P</span>', iconSize: [30,30], iconAnchor: [15,15] })
const destinationIcon = L.divIcon({ className: 'delivery-marker destination-marker', html: '<span>D</span>', iconSize: [30,30], iconAnchor: [15,15] })
type RouteResult = { distance: number; duration: number; coordinates: [number,number][]; calculatedAt: number }
function DeliveryMap({ donation, portion }: { donation: Donation; portion: Portion }) {
  const pickup = Number.isFinite(donation.lat) && Number.isFinite(donation.lon) ? [donation.lat,donation.lon] as [number,number] : null
  const destination = Number.isFinite(portion.destination_lat) && Number.isFinite(portion.destination_lon) ? [Number(portion.destination_lat),Number(portion.destination_lon)] as [number,number] : null
  const [route,setRoute]=useState<RouteResult|null>(null)
  const [loading,setLoading]=useState(false)
  const [error,setError]=useState('')
  useEffect(()=>{
    if(!pickup||!destination){setRoute(null);setLoading(false);setError('');return}
    const controller=new AbortController()
    setLoading(true);setRoute(null);setError('')
    const url=`${ROUTING_API}/route/v1/driving/${pickup[1]},${pickup[0]};${destination[1]},${destination[0]}?overview=full&geometries=geojson&steps=false`
    void fetch(url,{signal:controller.signal}).then(async response=>{if(!response.ok)throw new Error('The routing service is unavailable.');return response.json()}).then(data=>{
      const result=data.routes?.[0]
      if(data.code!=='Ok'||!result||!Number.isFinite(result.distance)||!Number.isFinite(result.duration)||!Array.isArray(result.geometry?.coordinates))throw new Error('No driving route is available for these locations.')
      setRoute({distance:result.distance,duration:result.duration,coordinates:result.geometry.coordinates.map(([lon,lat]:[number,number])=>[lat,lon]),calculatedAt:Date.now()})
    }).catch(reason=>{if(reason.name!=='AbortError')setError(reason instanceof Error?reason.message:'Could not load a driving route.')}).finally(()=>{if(!controller.signal.aborted)setLoading(false)})
    return ()=>controller.abort()
  },[donation.id,portion.id,pickup?.[0],pickup?.[1],destination?.[0],destination?.[1]])
  const points=[...(pickup?[pickup]:[]),...(destination?[destination]:[])]
  const routePoints=route?.coordinates ?? []
  const arrival=route?dateLabel(new Date(route.calculatedAt+route.duration*1000).toISOString()):''
  return <section className="delivery-route" aria-label={`Delivery route for ${donation.id}`}>
    <div className="delivery-route-heading"><div><div className="eyebrow">LISTING {donation.id}</div><h4>Delivery route · {portion.recipient_name}</h4></div><span className={`status ${portion.status}`}>{statusLabel(portion.status)}</span></div>
    <div className="delivery-location-labels"><span><b>Pickup</b>{donation.pickup_address}</span><span><b>Destination</b>{portion.recipient_address || 'Recipient address unavailable'}</span></div>
    {!pickup&&<p className="route-message">Pickup coordinates are missing. Edit the listing location to show a map.</p>}
    {pickup&&<div className="delivery-map"><MapContainer center={pickup} zoom={12} scrollWheelZoom={false} key={`${donation.id}-${portion.id}`}><TileLayer attribution='&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors' url="https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png" />{destination&&<Marker position={destination} icon={destinationIcon}><Popup>Destination · {portion.recipient_name}</Popup></Marker>}<Marker position={pickup} icon={pickupIcon}><Popup>Pickup · {donation.pickup_address}</Popup></Marker>{routePoints.length>0&&<Polyline positions={routePoints} pathOptions={{color:'#247151',weight:5,opacity:.85}}/>}{points.length>0&&<FitRoute points={routePoints.length>0?routePoints:points}/>}</MapContainer></div>}
    {!destination&&<p className="route-message">Recipient destination coordinates are missing. Update the organization location before estimating a route.</p>}
    {loading&&<p className="route-message" role="status">Loading driving route and travel estimate…</p>}
    {error&&<p className="route-message route-error" role="alert">{error} Distance and arrival time are unavailable.</p>}
    {route&&<div className="route-estimates"><span><b>{(route.distance/1000).toFixed(1)} km</b> route distance</span><span><b>{formatDuration(route.duration)}</b> estimated travel</span><span><b>{arrival}</b> estimated arrival</span></div>}
    {route&&<small className="estimate-note">Driving distance and time are calculated from the mapped pickup and destination coordinates. Travel time is an estimate and does not include live traffic, preparation, packing, or pickup delays.</small>}
  </section>
}
function FitRoute({ points }: { points:[number,number][] }) {
  const map=useMap()
  const signature=points.map(point=>point.join(',')).join(';')
  useEffect(()=>{if(points.length>1)map.fitBounds(points,{padding:[24,24]});else if(points.length===1)map.setView(points[0],13) },[map,signature])
  return null
}
function formatDuration(seconds:number) {
  const minutes=Math.max(1,Math.round(seconds/60))
  const hours=Math.floor(minutes/60)
  return hours ? `${hours} hr ${minutes%60} min` : `${minutes} min`
}
function Empty({ title, text }: { title: string; text: string }) { return <div className="empty-state"><span>✿</span><h3>{title}</h3><p>{text}</p></div> }

export default App

