// Provide a stateful offline Telegram bridge and ServiceHub API contract for browser journeys.

import type {Page, Route} from '@playwright/test';

export type Offer = {
  id: string;
  revision: number;
  price_cents: number;
  status: string;
  driver_name: string;
};

export type Ride = {
  id: string;
  state: string;
  revision: number;
  generation: number;
  role: 'rider' | 'driver' | 'observer';
  pickup: string;
  destination: string;
  scheduled_label: string;
  price_cents: number | null;
  offers: Offer[];
  bid_until: number;
  choose_until: number;
  exact_pickup?: string;
  exact_destination?: string;
  unit?: string;
  instructions?: string;
  pickup_attempt?: string;
};

export type MockOptions = {
  ride?: Ride | null;
  sessionError?: string;
  addressError?: string;
  commandError?: string;
  locationError?: string;
  counterpart?: {latitude: number; longitude: number; accuracy: number; sampled_at: number; stale?: boolean} | null;
};

export type MockState = {
  ride: Ride | null;
  draftBody: Record<string, unknown> | null;
  commands: Array<{action: string; args: Record<string, unknown>}>;
  locationPosts: Array<Record<string, unknown>>;
};

// Build a complete ride response matching the backend's role-filtered API shape.
export function ride(overrides: Partial<Ride> = {}): Ride {
  const now = Date.now() / 1000;
  return {
    id: '17ff5d41c20a4d4b8fc31a13f36f97aa',
    state: 'Open',
    revision: 2,
    generation: 1,
    role: 'rider',
    pickup: 'Sherrington Drive, Thunder Bay',
    destination: 'Arthur Street West, Thunder Bay',
    scheduled_label: 'Immediate',
    price_cents: null,
    offers: [],
    bid_until: now + 300,
    choose_until: now + 600,
    exact_pickup: '10 Sherrington Drive, Thunder Bay, ON',
    exact_destination: '200 Arthur Street West, Thunder Bay, ON',
    unit: '4B',
    instructions: 'Meet in the lobby',
    ...overrides,
  };
}

// Install Telegram's initialization and permission-aware location surface before application code runs.
export async function installTelegram(
  page: Page,
  location: {latitude: number; longitude: number; horizontal_accuracy?: number} | null = {
    latitude: 48.4,
    longitude: -89.25,
    horizontal_accuracy: 12,
  },
): Promise<void> {
  await page.route('https://telegram.org/**', route => route.abort());
  await page.addInitScript(fix => {
    Object.defineProperty(window, 'Telegram', {
      configurable: true,
      value: {
        WebApp: {
          initData: 'signed-test-init-data',
          ready: () => undefined,
          expand: () => undefined,
          LocationManager: {
            init: (callback: () => void) => callback(),
            getLocation: (callback: (value: typeof fix) => void) => callback(fix),
          },
        },
      },
    });
  }, location);
}

// Fulfill one intercepted API response with the JSON contract used by the Mini App.
async function json(route: Route, body: unknown, status = 200): Promise<void> {
  await route.fulfill({
    status,
    contentType: 'application/json',
    body: JSON.stringify(body),
  });
}

// Intercept every backend request with stateful responses and visible configurable failures.
export async function mockServiceHub(page: Page, options: MockOptions = {}): Promise<MockState> {
  const state: MockState = {
    ride: options.ride === undefined ? null : options.ride,
    draftBody: null,
    commands: [],
    locationPosts: [],
  };
  let pendingAction = '';

  await page.route('**/api/**', async route => {
    const request = route.request();
    const url = new URL(request.url());
    const path = url.pathname;
    const method = request.method();

    if (path === '/api/session' && method === 'POST') {
      if (options.sessionError) return json(route, {detail: options.sessionError}, 401);
      return json(route, {token: 'test-bearer-token'});
    }
    if (path === '/api/rides/current' && method === 'GET') return json(route, state.ride);
    if (/^\/api\/rides\/[^/]+$/.test(path) && method === 'GET') return json(route, state.ride);
    if (path === '/api/places' && method === 'GET') {
      if (options.addressError) return json(route, {detail: options.addressError}, 503);
      const query = url.searchParams.get('query') ?? '';
      const destination = query.toLowerCase().includes('arthur');
      return json(route, [{id: destination ? 'destination' : 'pickup', text: `${query} suggestion`}]);
    }
    if (path.startsWith('/api/places/') && method === 'GET') {
      if (options.addressError) return json(route, {detail: options.addressError}, 503);
      const destination = path.endsWith('/destination');
      return json(route, {
        token: destination ? 'signed-destination' : 'signed-pickup',
        label: destination
          ? '200 Arthur Street West, Thunder Bay, ON'
          : '10 Sherrington Drive, Thunder Bay, ON',
      });
    }
    if (path === '/api/rides/draft' && method === 'POST') {
      state.draftBody = request.postDataJSON() as Record<string, unknown>;
      return json(route, {id: '17ff5d41c20a4d4b8fc31a13f36f97aa', revision: 1});
    }
    if (path === '/api/commands' && method === 'POST') {
      if (options.commandError) return json(route, {detail: options.commandError}, 409);
      const command = request.postDataJSON() as {action: string; args: Record<string, unknown>};
      state.commands.push(command);
      pendingAction = command.action;
      return json(route, {id: `command-${state.commands.length}`, summary: `Review ${command.action} for this ride.`});
    }
    if (/^\/api\/commands\/[^/]+\/confirm$/.test(path) && method === 'POST') {
      if (options.commandError) return json(route, {detail: options.commandError}, 409);
      if (pendingAction === 'publish') state.ride = ride();
      if (state.ride && pendingAction === 'accept') {
        state.ride = {...state.ride, state: 'Matched', price_cents: 2500, offers: state.ride.offers.map(item => ({...item, status: 'accepted'}))};
      }
      if (state.ride && pendingAction === 'depart') state.ride = {...state.ride, state: 'Driver_En_Route'};
      if (state.ride && pendingAction === 'start') state.ride = {...state.ride, state: 'Pickup_Confirmation_Pending'};
      if (state.ride && pendingAction === 'pickup') state.ride = {...state.ride, state: 'Trip_Started'};
      if (state.ride && pendingAction === 'complete') state.ride = {...state.ride, state: 'Completed'};
      return json(route, state.ride);
    }
    if (/^\/api\/rides\/[^/]+\/location$/.test(path) && method === 'POST') {
      if (options.locationError) return json(route, {detail: options.locationError}, 409);
      state.locationPosts.push(request.postDataJSON() as Record<string, unknown>);
      return json(route, {ok: true});
    }
    if (/^\/api\/rides\/[^/]+\/location$/.test(path) && method === 'GET') {
      if (options.locationError) return json(route, {detail: options.locationError}, 409);
      return json(route, options.counterpart ?? null);
    }
    return json(route, {detail: `Unhandled test route: ${method} ${path}`}, 500);
  });
  return state;
}

// Select a mocked Ontario address through the same debounced UI used by users.
export async function selectAddress(page: Page, label: string, query: string): Promise<void> {
  const input = page.getByLabel(label);
  await input.fill(query);
  await page.getByRole('button', {name: `${query} suggestion`}).click();
}
