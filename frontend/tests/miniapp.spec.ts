// Exercise primary rider, driver, scheduling, offer, tracking, and failure journeys in Chromium.

import {expect, test} from '@playwright/test';

import {installTelegram, mockServiceHub, ride, selectAddress} from './support/servicehub-api';


test('outside Telegram shows secure sign-in and disables submission', async ({page}) => {
  // Verify a normal browser cannot impersonate a Telegram user.
  await page.route('https://telegram.org/**', route => route.abort());
  await mockServiceHub(page);
  await page.goto('/');
  await expect(page.getByRole('alert')).toContainText('Open ServiceHub using Need a Ride in Telegram');
  await expect(page.getByRole('button', {name: /Review ride request/})).toBeDisabled();
});


test('authenticated rider creates and confirms an immediate ride', async ({page}) => {
  // Complete address selection, immutable review, explicit confirmation, and active-state refresh.
  await installTelegram(page);
  const state = await mockServiceHub(page);
  await page.goto('/');
  await selectAddress(page, 'Pickup address', 'Sherrington');
  await selectAddress(page, 'Destination address', 'Arthur');
  await page.getByLabel('Unit / apartment').fill('4B');
  await page.getByLabel('Pickup municipality').fill('Thunder Bay');
  await page.getByLabel('Pickup instructions').fill('Meet in the lobby');
  await page.getByRole('button', {name: /Review ride request/}).click();
  await expect(page.getByRole('dialog')).toContainText('Review publish for this ride.');
  await page.getByRole('button', {name: 'Confirm'}).click();
  await expect(page.getByText('Open', {exact: true})).toBeVisible();
  expect(state.draftBody).toMatchObject({
    pickup_token: 'signed-pickup',
    destination_token: 'signed-destination',
    training_city: 'Thunder Bay',
    unit: '4B',
    instructions: 'Meet in the lobby',
    local_time: null,
  });
  expect(state.commands[0].action).toBe('publish');
});


test('authenticated rider submits the scheduled form in one flow', async ({page}) => {
  // Preserve the selected Ontario timezone and local pickup time in the draft contract.
  await installTelegram(page);
  const state = await mockServiceHub(page);
  await page.goto('/');
  await page.getByRole('button', {name: /Schedule/}).click();
  await page.getByLabel('Pickup date & time').fill('2026-11-20T12:30');
  await page.getByLabel('Timezone').selectOption('America/Toronto');
  await selectAddress(page, 'Pickup address', 'Sherrington');
  await selectAddress(page, 'Destination address', 'Arthur');
  await page.getByLabel('Pickup municipality').fill('Thunder Bay');
  await page.getByRole('button', {name: /Review ride request/}).click();
  expect(state.draftBody).toMatchObject({
    local_time: '2026-11-20T12:30',
    timezone: 'America/Toronto',
  });
});


test('rider reviews and accepts an offer and sees API validation errors', async ({page}) => {
  // Show driver and price facts before confirmation, then surface a rejected action accessibly.
  await installTelegram(page);
  const state = await mockServiceHub(page, {
    ride: ride({
      offers: [{id: 'offer-1', revision: 1, price_cents: 2500, status: 'pending', driver_name: 'Avery'}],
    }),
  });
  await page.goto('/');
  await expect(page.getByText('Avery')).toBeVisible();
  await expect(page.getByText('CAD 25.00')).toBeVisible();
  await page.getByRole('button', {name: 'Accept Offer'}).click();
  await expect(page.getByRole('dialog')).toContainText('Review accept');
  await page.getByRole('button', {name: 'Confirm'}).click();
  await expect(page.getByText('Matched', {exact: true})).toBeVisible();
  expect(state.commands[0].args).toMatchObject({offer_id: 'offer-1', offer_revision: 1});

  const errorPage = await page.context().newPage();
  await installTelegram(errorPage);
  await mockServiceHub(errorPage, {
    ride: ride({offers: [{id: 'offer-2', revision: 1, price_cents: 3000, status: 'pending', driver_name: 'Jordan'}]}),
    commandError: 'Offer changed; refresh before accepting',
  });
  await errorPage.goto('/');
  await errorPage.getByRole('button', {name: 'Accept Offer'}).click();
  await expect(errorPage.getByRole('alert')).toContainText('Offer changed');
});


test('driver advances matched, pickup, and completion controls', async ({page}) => {
  // Verify driver controls follow authoritative server state after each explicit confirmation.
  await installTelegram(page);
  const state = await mockServiceHub(page, {ride: ride({role: 'driver', state: 'Matched', price_cents: 2500})});
  await page.goto('/');
  await page.getByRole('button', {name: 'On My Way'}).click();
  await page.getByRole('button', {name: 'Confirm'}).click();
  await expect(page.getByText('Driver En Route', {exact: true})).toBeVisible();
  await page.getByRole('button', {name: /Start Trip/}).click();
  await page.getByRole('button', {name: 'Confirm'}).click();
  await expect(page.getByText('Pickup Confirmation Pending', {exact: true})).toBeVisible();

  state.ride = ride({role: 'driver', state: 'Trip_Started', price_cents: 2500});
  await page.reload();
  await page.getByRole('button', {name: /Complete Trip/}).click();
  await page.getByRole('button', {name: 'Confirm'}).click();
  await expect(page.getByText('Completed', {exact: true})).toBeVisible();
  expect(state.commands.map(command => command.action)).toEqual(['depart', 'start', 'complete']);
});


test('tracking requires consent, publishes Telegram location, and stops on terminal state', async ({page}) => {
  // Publish only after the sharing button and render fresh counterpart location status.
  await installTelegram(page, {latitude: 48.401, longitude: -89.251, horizontal_accuracy: 9});
  const state = await mockServiceHub(page, {
    ride: ride({role: 'rider', state: 'Driver_En_Route'}),
    counterpart: {latitude: 48.402, longitude: -89.252, accuracy: 12, sampled_at: Date.now() / 1000},
  });
  await page.goto('/');
  expect(state.locationPosts).toHaveLength(0);
  await page.getByRole('button', {name: 'Share My Location'}).click();
  await expect.poll(() => state.locationPosts.length).toBe(1);
  expect(state.locationPosts[0]).toMatchObject({latitude: 48.401, longitude: -89.251, accuracy: 9});
  await page.getByRole('button', {name: 'View Driver Location'}).click();
  await expect(page.getByText('Recent location')).toBeVisible();
  state.ride = ride({role: 'rider', state: 'Completed', price_cents: 2500});
  await page.reload();
  await expect(page.getByRole('button', {name: 'Share My Location'})).toHaveCount(0);
});


test('session, address, and location failures appear as accessible mobile notices', async ({page}) => {
  // Keep critical failures visible at the configured Telegram-like viewport.
  await installTelegram(page);
  await mockServiceHub(page, {sessionError: 'Telegram session expired'});
  await page.goto('/');
  await expect(page.getByRole('alert')).toContainText('Telegram session expired');
  expect(page.viewportSize()).toEqual({width: 390, height: 844});

  const addressPage = await page.context().newPage();
  await installTelegram(addressPage);
  await mockServiceHub(addressPage, {addressError: 'Address lookup unavailable'});
  await addressPage.goto('/');
  await addressPage.getByLabel('Pickup address').fill('Sherrington');
  await expect(addressPage.getByRole('alert')).toContainText('Address lookup unavailable');

  const locationPage = await page.context().newPage();
  await installTelegram(locationPage);
  await mockServiceHub(locationPage, {
    ride: ride({role: 'rider', state: 'Driver_En_Route'}),
    locationError: 'Location access is unavailable',
  });
  await locationPage.goto('/');
  await locationPage.getByRole('button', {name: 'View Driver Location'}).click();
  await expect(locationPage.getByRole('alert')).toContainText('Location access is unavailable');
});
