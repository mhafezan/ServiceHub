// Render ServiceHub's mobile ride form, confirmed actions, and consent-based tracking.

import React, { useCallback, useEffect, useRef, useState } from 'react';
import { createRoot } from 'react-dom/client';
import './style.css';

type Offer = {id: string; revision: number; price_cents: number; status: string; driver_name: string};
type Ride = {id: string; state: string; revision: number; generation: number; role: string; pickup: string; destination: string; scheduled_label: string; price_cents: number | null; offers: Offer[]; exact_pickup?: string; exact_destination?: string; unit?: string; instructions?: string; bid_until: number; choose_until: number; pickup_attempt?: string};
type Fix = {latitude: number; longitude: number; accuracy: number | null; sampled_at: number; stale?: boolean};
type Selection = {token: string; label: string};
type TG = {initData: string; ready: () => void; expand: () => void; LocationManager?: {init: (cb: () => void) => void; getLocation: (cb: (data: {latitude: number; longitude: number; horizontal_accuracy?: number} | null) => void) => void}};
declare global {interface Window {Telegram?: {WebApp: TG}}}

let token = '';
// Call the authenticated backend while retaining structured failure messages.
async function api<T>(path: string, method = 'GET', body?: unknown): Promise<T> {
  const response = await fetch(path, {method, headers: {'Content-Type': 'application/json', Authorization: `Bearer ${token}`}, body: body === undefined ? undefined : JSON.stringify(body)});
  const data = await response.json();
  if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : 'Please check the form and try again.');
  return data as T;
}

// Provide an accessible address picker that invalidates a selection whenever its text changes.
function AddressPicker({label, onSelect}: {label: string; onSelect: (selection: Selection | null) => void}) {
  const [query, setQuery] = useState('');
  const [rows, setRows] = useState<{id: string; text: string}[]>([]);
  const [selected, setSelected] = useState(false);
  const [error, setError] = useState('');
  const session = useRef(crypto.randomUUID());
  useEffect(() => {
    
    // Debounce provider requests and ignore results from superseded queries.
    let active = true;
    if (query.length < 3 || selected) {setRows([]); return;}
    const timeout = setTimeout(() => {
      api<{id: string; text: string}[]>(`/api/places?query=${encodeURIComponent(query)}&session_token=${session.current}`)
        .then(data => {if (active) setRows(data);}).catch(err => {if (active) setError(err.message);});
    }, 350);
    return () => {active = false; clearTimeout(timeout);};
  }, [query, selected]);
  
  // Resolve a provider choice into a server-signed Ontario selection.
  async function choose(id: string) {
    try {const result = await api<Selection>(`/api/places/${id}`); setQuery(result.label); setSelected(true); setRows([]); onSelect(result); setError('');}
    catch (err) {setError((err as Error).message);}
  }
  return <div className="field"><label>{label}<input value={query} placeholder="Search an Ontario address" autoComplete="off" onChange={event => {setQuery(event.target.value); setSelected(false); onSelect(null); setError('');}}/></label>
    {rows.length > 0 && <ul className="suggestions">{rows.map(row => <li key={row.id}><button type="button" onClick={() => void choose(row.id)}>{row.text}</button></li>)}</ul>}
    {selected && <small className="verified">✓ Ontario address selected</small>}{error && <small role="alert">{error}</small>}
  </div>;
}

// Coordinate the ride form, active booking, explicit confirmations, and foreground location consent.
function App() {
  const [ready, setReady] = useState(false);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const [ride, setRide] = useState<Ride | null>(null);
  const [tab, setTab] = useState<'request' | 'ride'>('request');
  const [scheduled, setScheduled] = useState(false);
  const [pickup, setPickup] = useState<Selection | null>(null);
  const [destination, setDestination] = useState<Selection | null>(null);
  const [proposal, setProposal] = useState<{id: string; summary: string} | null>(null);
  const [sharing, setSharing] = useState(false);
  const [fix, setFix] = useState<Fix | null>(null);
  const [ownFix, setOwnFix] = useState<Fix | null>(null);
  const [clock, setClock] = useState(Date.now());
  const rideId = useRef(new URLSearchParams(location.search).get('ride'));
  const form = useRef<HTMLFormElement>(null);
  
  // Refresh only the selected or owned ride, preserving server authorization boundaries.
  const refresh = useCallback(async () => {
    const result = await api<Ride | null>(rideId.current ? `/api/rides/${rideId.current}` : '/api/rides/current');
    setRide(result);
    if (result) {setTab('ride'); rideId.current = result.id;}
  }, []);
  useEffect(() => {
    
    // Authenticate from Telegram; a browser preview cannot impersonate a user.
    const telegram = window.Telegram?.WebApp;
    telegram?.ready(); telegram?.expand();
    if (!telegram?.initData) {setError('Open ServiceHub using Need a Ride in Telegram to sign in.'); return;}
    api<{token: string}>('/api/session', 'POST', {init_data: telegram.initData})
      .then(async result => {token = result.token; setReady(true); await refresh();})
      .catch(err => setError(err.message));
  }, [refresh]);
  useEffect(() => {
    
    // Keep the visible status and countdown current while the app remains open.
    if (!ready) return;
    const timer = setInterval(() => {setClock(Date.now()); void refresh().catch(() => {});}, 5000);
    return () => clearInterval(timer);
  }, [ready, refresh]);
  const trackable = !!ride && ['Driver_En_Route', 'Pickup_Confirmation_Pending', 'Trip_Started'].includes(ride.state) && ride.role !== 'observer';
  useEffect(() => {
    
    // Stop location collection immediately when the assignment's tracking window closes.
    if (!trackable) {setSharing(false); setFix(null); setOwnFix(null);}
  }, [trackable]);
  useEffect(() => {
    
    // Send GPS fixes only after explicit consent and only while this view is foregrounded.
    if (!sharing || !ride || !trackable) return;
    let stopped = false;
    
    // Persist one acquired device sample without retaining a browser location history.
    async function publish(latitude: number, longitude: number, accuracy: number | null) {
      if (stopped) return;
      const current = {latitude, longitude, accuracy, sampled_at: Date.now() / 1000};
      try {await api(`/api/rides/${ride!.id}/location`, 'POST', current); setOwnFix(current);}
      catch (err) {setError((err as Error).message);}
    }
    
    // Prefer Telegram's permission-aware location bridge and fall back to browser geolocation.
    function sample() {
      if (document.visibilityState !== 'visible') return;
      const manager = window.Telegram?.WebApp.LocationManager;
      if (manager) manager.init(() => manager.getLocation(data => {
        if (data) void publish(data.latitude, data.longitude, data.horizontal_accuracy ?? null);
        else {setSharing(false); setError('Location permission is required for sharing.');}
      }));
      else navigator.geolocation?.getCurrentPosition(position => void publish(position.coords.latitude, position.coords.longitude, position.coords.accuracy), () => {setSharing(false); setError('Location permission is unavailable.');}, {enableHighAccuracy: true, maximumAge: 0, timeout: 15000});
    }
    sample(); const timer = setInterval(sample, 10000);
    return () => {stopped = true; clearInterval(timer);};
  }, [sharing, ride?.id, trackable]);
  
  // Prepare the server's immutable summary for an explicit second confirmation.
  async function propose(action: string, args: Record<string, unknown>) {
    setBusy(true); setError('');
    try {setProposal(await api('/api/commands', 'POST', {action, args}));}
    catch (err) {setError((err as Error).message);}
    finally {setBusy(false);}
  }
  
  // Save the single-form draft before presenting the publish confirmation.
  async function submit(event: React.FormEvent) {
    event.preventDefault(); if (!form.current || !pickup || !destination) return;
    setBusy(true); setError('');
    const data = new FormData(form.current);
    try {
      const draft = await api<{id: string}>('/api/rides/draft', 'POST', {pickup_token: pickup.token, destination_token: destination.token, training_city: data.get('city'), unit: data.get('unit'), instructions: data.get('instructions'), local_time: scheduled ? data.get('time') : null, timezone: data.get('timezone') || 'America/Toronto'});
      rideId.current = draft.id; await propose('publish', {ride_id: draft.id});
    } catch (err) {setError((err as Error).message);} finally {setBusy(false);}
  }
  
  // Execute a user-confirmed command once and restore current server state.
  async function confirm() {
    if (!proposal) return; setBusy(true);
    try {await api(`/api/commands/${proposal.id}/confirm`, 'POST'); setProposal(null); await refresh();}
    catch (err) {setError((err as Error).message);} finally {setBusy(false);}
  }
  
  // Refresh the counterpart's location without silently starting the caller's own tracking.
  async function viewLocation() {
    try {const result = await api<Fix | null>(`/api/rides/${ride?.id}/location`); setFix(result); if (!result) setError('No shared location is available yet.');}
    catch (err) {setError((err as Error).message);}
  }
  const remaining = ride ? Math.max(0, Math.ceil(((ride.state === 'Open' ? ride.bid_until : ride.choose_until) * 1000 - clock) / 1000)) : 0;
  return <main>
    <header><div className="brand"><span className="mark">S</span>ServiceHub<span className="province">ONTARIO</span></div><a href="/guides/ride" target="_blank" rel="noreferrer">Quick guide ↗</a></header>
    <section className="hero"><span className="eyebrow">GOOD CONNECTIONS. BETTER JOURNEYS.</span><h1>A ride, made simple.</h1><p>Your community. Your destination.<br/>Find your next ride across Ontario.</p><div className="hero-line"><span>↗</span><span className="dot"/><span className="dot"/><span className="dot"/><span>◎</span></div></section>
    <nav aria-label="Ride navigation"><button className={tab === 'request' ? 'active' : ''} onClick={() => setTab('request')}>Need a Ride</button><button className={tab === 'ride' ? 'active' : ''} onClick={() => setTab('ride')}>My Ride{ride && <span className="badge">1</span>}</button></nav>
    {error && <div className="notice" role="alert">{error}<button aria-label="Dismiss message" onClick={() => setError('')}>×</button></div>}
    {!ready && <p className="muted">Secure sign-in happens inside Telegram. No separate account needed.</p>}
    {tab === 'request' && <form ref={form} onSubmit={event => void submit(event)} className="card">
      <div className="section-heading"><span className="step">01</span><div><h2>Plan your ride</h2><p>Now or later, we’ll connect you.</p></div></div>
      <div className="segmented"><button type="button" className={!scheduled ? 'chosen' : ''} onClick={() => setScheduled(false)}>↗ Ride now</button><button type="button" className={scheduled ? 'chosen' : ''} onClick={() => setScheduled(true)}>◷ Schedule</button></div>
      {scheduled && <div className="schedule"><label>Pickup date & time<input name="time" type="datetime-local" required/></label><label>Timezone<select name="timezone"><option>America/Toronto</option><option>America/Winnipeg</option><option>America/Atikokan</option></select></label><small>30 minutes to 30 days ahead. Offers are collected now.</small></div>}
      <AddressPicker label="Pickup address" onSelect={setPickup}/><AddressPicker label="Destination address" onSelect={setDestination}/>
      <small className="attribution">Address search powered by Google</small>
      <div className="columns"><label>Unit / apartment <span>(private)</span><input name="unit" maxLength={100} placeholder="Optional"/></label><label>Pickup municipality<input name="city" required minLength={2} maxLength={100} placeholder="Enter your city"/></label></div>
      <small>Enter your municipality yourself. Completed trip distance and price may be used for city-specific pricing research.</small>
      <label>Pickup instructions <span>(private)</span><textarea name="instructions" maxLength={500} placeholder="Entrance, meeting point, or anything useful"/></label>
      <div className="privacy">◇ Only your street and municipality appear in the channel. Your selected driver receives the full address.</div>
      <button className="primary" disabled={!ready || !pickup || !destination || busy}>Review ride request <span>→</span></button>
      <p className="footnote">5 minutes for offers · CAD prices · One active booking</p>
    </form>}
    {tab === 'ride' && <section className="card">
      {!ride ? <div className="empty"><span>◎</span><h2>No active ride yet</h2><p>Your next journey starts with a request.</p><button className="primary" onClick={() => setTab('request')}>Plan a ride →</button></div> : <>
        <div className="status-row"><span className="status">{ride.state.replaceAll('_', ' ')}</span><small>#{ride.id.slice(0, 8)}</small></div>
        <h2>{ride.pickup} <span className="route-arrow">→</span> {ride.destination}</h2><p className="muted">{ride.scheduled_label}</p>
        {ride.exact_pickup && <div className="private-details"><p><strong>Pickup:</strong> {ride.exact_pickup} {ride.unit}</p><p><strong>Destination:</strong> {ride.exact_destination}</p>{ride.instructions && <p>{ride.instructions}</p>}</div>}
        {['Open', 'Selecting'].includes(ride.state) && <div className="countdown">◷ {Math.floor(remaining / 60)}:{String(remaining % 60).padStart(2, '0')} {ride.state === 'Open' ? 'left for offers' : 'left to choose'}</div>}
        {ride.price_cents !== null && <p className="price">CAD {(ride.price_cents / 100).toFixed(2)}</p>}
        <div className="offers">{ride.offers.map(offer => <div className="offer" key={offer.id}><div><strong>{offer.driver_name}</strong><small>{offer.status}</small></div><b>CAD {(offer.price_cents / 100).toFixed(2)}</b>{ride.role === 'rider' && offer.status === 'pending' && <button onClick={() => void propose('accept', {ride_id: ride.id, offer_id: offer.id, offer_revision: offer.revision})}>Accept Offer</button>}{ride.role === 'observer' && offer.status === 'pending' && <button onClick={() => void propose('withdraw', {ride_id: ride.id, offer_id: offer.id})}>Withdraw</button>}</div>)}</div>
        {ride.role === 'observer' && ride.state === 'Open' && <form onSubmit={event => {event.preventDefault(); const value = String(new FormData(event.currentTarget).get('amount')); if (!/^\d+(\.\d{1,2})?$/.test(value)) {setError('Enter a price with at most two decimals.'); return;} void propose('offer', {ride_id: ride.id, price_cents: Math.round(Number(value) * 100)});}}><label>Your offer (CAD)<input name="amount" inputMode="decimal" required placeholder="25.00"/></label><button className="primary">Review Offer</button></form>}
        <div className="actions">
          {ride.role === 'driver' && ride.state === 'Matched' && <button className="primary" onClick={() => void propose('depart', {ride_id: ride.id})}>On My Way</button>}
          {ride.role === 'driver' && ride.state === 'Driver_En_Route' && <button onClick={() => void propose('start', {ride_id: ride.id})}>Start Trip · requires nearby location</button>}
          {ride.role === 'rider' && ride.state === 'Pickup_Confirmation_Pending' && <><button className="primary" onClick={() => void propose('pickup', {ride_id: ride.id, accepted: true})}>Confirm Pickup</button><button onClick={() => void propose('pickup', {ride_id: ride.id, accepted: false})}>Not Picked Up</button></>}
          {ride.role === 'driver' && ride.state === 'Trip_Started' && <><button className="primary" onClick={() => void propose('complete', {ride_id: ride.id})}>Complete Trip · requires nearby location</button><small>Complete this trip at the destination before making another offer.</small></>}
          {ride.role === 'rider' && ride.state === 'Awaiting_Rider' && <button onClick={() => void propose('reopen', {ride_id: ride.id})}>Reopen Request</button>}
          {ride.role !== 'observer' && !['Completed', 'Cancelled', 'Expired', 'Trip_Started', 'Administrative_Closure'].includes(ride.state) && <button className="danger" onClick={() => void propose('cancel', {ride_id: ride.id})}>Cancel Ride</button>}
          {['Completed', 'Cancelled', 'Expired', 'Administrative_Closure'].includes(ride.state) && <button onClick={() => {rideId.current = null; setRide(null); setTab('request');}}>Request Another Ride</button>}
        </div>
        {trackable && <div className="tracking"><h3>Location sharing</h3><p>Share only while this app is open, or share live location in your private bot chat.</p><button onClick={() => setSharing(!sharing)}>{sharing ? 'Stop Mini App Sharing' : 'Share My Location'}</button><button onClick={() => void viewLocation()}>View {ride.role === 'rider' ? 'Driver' : 'Rider'} Location</button>{ownFix && <small>Your last fix: {new Date(ownFix.sampled_at * 1000).toLocaleTimeString()} · accuracy {ownFix.accuracy ?? 'unknown'} m</small>}{fix && <div><strong>{fix.stale || Date.now() / 1000 - fix.sampled_at > 60 ? 'Stale location' : 'Recent location'}</strong><p>{fix.latitude.toFixed(5)}, {fix.longitude.toFixed(5)} · {new Date(fix.sampled_at * 1000).toLocaleTimeString()}</p><a href={`https://www.google.com/maps?q=${fix.latitude},${fix.longitude}`} target="_blank" rel="noreferrer">Open location on map ↗</a></div>}</div>}
      </>}
    </section>}
    {proposal && <div className="modal-backdrop"><section role="dialog" aria-modal="true" aria-labelledby="confirmation-title" className="modal"><span className="eyebrow">ONE LAST CHECK</span><h2 id="confirmation-title">Confirm your action</h2><p>{proposal.summary}</p><button className="primary" disabled={busy} onClick={() => void confirm()}>{busy ? 'Working…' : 'Confirm'}</button><button disabled={busy} onClick={() => setProposal(null)}>Go back</button>{error && <p role="alert">{error}</p>}</section></div>}
    <footer>ServiceHub · Community connections, thoughtfully made.<br/><a href="/guides/ride">How it works</a> · <a href="/privacy.html">Privacy</a> · <a href="/terms.html">Terms</a></footer>
  </main>;
}

createRoot(document.getElementById('root')!).render(<App/>);
