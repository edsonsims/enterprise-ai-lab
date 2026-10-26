/** Real Chromium, actual local OIDC and rendered admin UI; no API mocks. */
import assert from 'node:assert/strict';
import {constants, openSync, fstatSync, readFileSync, closeSync, mkdirSync, writeFileSync, existsSync} from 'node:fs';
import {dirname, join, resolve} from 'node:path';
import {chromium} from 'playwright';

const [portalArgument, accountsArgument, outputArgument] = process.argv.slice(2);
assert.equal(process.argv.length, 5);
const portal = new URL(portalArgument);
assert.ok(portal.protocol === 'http:' && portal.hostname === '127.0.0.1' && portal.port
  && !portal.username && !portal.password && !portal.search && !portal.hash && portal.pathname === '/');
const origin = portal.origin;
const output = resolve(outputArgument);
assert.equal(existsSync(output), false);
const fd = openSync(accountsArgument, constants.O_RDONLY | constants.O_NOFOLLOW | constants.O_NONBLOCK);
let accounts;
try {
  const stat = fstatSync(fd);
  assert.ok(stat.isFile() && stat.size <= 32768 && (stat.mode & 0o777) === 0o600);
  if (process.getuid) assert.equal(stat.uid, process.getuid());
  const value = JSON.parse(new TextDecoder('utf-8', {fatal: true}).decode(readFileSync(fd)));
  assert.equal(value.schema_version, 1);
  accounts = new Map(value.accounts.map((entry) => [entry.username, entry.password]));
  for (const name of ['admin-alpha', 'admin-beta', 'auditor-alpha']) assert.ok(accounts.has(name));
} finally { closeSync(fd); }

const checks = [];
const report = {schema_version: 1, recorded_at: new Date().toISOString(), passed: false,
  experiment: 'Actual Chromium local configuration desk', checks,
  human_review_performed: false,
  limitations: ['Automated fixture-account actions are not owner review or an accessibility certification.',
    'The separate HTTP/process experiment proves actual before/after-commit kill and recovery; this script does not kill a service.']};
let browser;
let context;
let page;
let phase = 'launch';
let configReceived = false;
let initialConfigPending = true;
let releaseConfig = () => {};
// Diagnostic metadata is limited to the first initial-view GET per endpoint.
// Associate requests before responses arrive, so a prior login cannot supply it.
const viewFields = ['config_status', 'work_orders_status', 'audit_status'];
const viewEndpoints = new Map([
  [origin + '/api/config', 'config_status'],
  [origin + '/api/work-orders', 'work_orders_status'],
  [origin + '/api/audit', 'audit_status'],
]);
const viewErrorCodes = new Set(['unauthenticated', 'forbidden', 'invalid_request',
  'conflict', 'busy', 'storage_unavailable', 'capacity', 'not_found']);
const loginViewStages = new Map([
  ['admin-alpha', 'login_view_admin'],
  ['auditor-alpha', 'login_view_auditor'],
  ['admin-beta', 'login_view_beta'],
]);
let viewGeneration = 0;
let viewActive = false;
let viewContext;
let viewErrors;
let viewStarted;
const viewRequests = new WeakMap();
function resetViewContext() {
  viewGeneration += 1;
  viewActive = true;
  viewContext = {config_status: 0, work_orders_status: 0, audit_status: 0};
  viewErrors = {config_status: 'unknown', work_orders_status: 'unknown', audit_status: 'unknown'};
  viewStarted = new Set();
}
function observeViewRequest(request) {
  const field = viewEndpoints.get(request.url());
  if (!viewActive || request.method() !== 'GET' || !field || viewStarted.has(field)) return;
  viewStarted.add(field);
  viewRequests.set(request, {generation: viewGeneration, field});
}
async function observeViewResponse(response) {
  const observed = viewRequests.get(response.request());
  if (!observed || !viewActive || observed.generation !== viewGeneration) return;
  const status = response.status();
  const field = observed.field;
  viewContext[field] = Number.isInteger(status) && status >= 100 && status <= 599 ? status : 0;
  if (status < 400 || status > 599) return;
  let code = 'unknown';
  try {
    const value = await response.json();
    if (typeof value?.error?.code === 'string' && viewErrorCodes.has(value.error.code)) code = value.error.code;
  } catch { /* Only a fixed unknown marker is retained. */ }
  if (viewActive && observed.generation === viewGeneration) viewErrors[field] = code;
}
function failureViewContext() {
  const value = viewContext || {config_status: 0, work_orders_status: 0, audit_status: 0};
  const failed = viewFields.find((field) => value[field] >= 400);
  const code = failed ? viewErrors[failed] : viewFields.every((field) => value[field] !== 0) ? 'none' : 'unknown';
  return {...value, error_code: code};
}
const record = (name, detail = {}) => checks.push({name, passed: true, ...detail});
async function until(operation) {
  const deadline = Date.now() + 15000;
  while (Date.now() < deadline) {
    if (await operation()) return;
    await new Promise((done) => setTimeout(done, 60));
  }
  throw new Error('Bounded browser condition did not become true.');
}
async function login(name) {
  assert.ok(loginViewStages.has(name));
  resetViewContext();
  phase = 'login_navigation';
  await page.goto(origin + '/');
  await page.locator('#login').waitFor({state: 'visible'});
  await page.locator('#login').focus();
  await page.keyboard.press('Enter');
  phase = 'login_form';
  await page.locator('input[name="username"]').waitFor({state: 'visible'});
  await page.locator('input[name="username"]').fill(name);
  await page.locator('input[name="password"]').fill(accounts.get(name));
  phase = 'login_redirect';
  await page.getByRole('button', {name: 'Sign in', exact: true}).click();
  await until(async () => {
    if (page.url().startsWith(origin + '/') && (await page.locator('#identity').textContent()).includes(name)) return true;
    const button = page.getByRole('button', {name: 'Continue', exact: true});
    if (await button.isVisible()) await button.click();
    return false;
  });
  if (initialConfigPending) {
    phase = 'initial_view_guard';
    try {
      await until(async () => configReceived);
      assert.equal(await page.locator('#desk').isVisible(), false);
      assert.equal(await page.locator('#revision').isVisible(), false);
    } finally {
      initialConfigPending = false;
      releaseConfig();
    }
  }
  phase = loginViewStages.get(name);
  await page.locator('#revision').waitFor({state: 'visible'});
  viewActive = false;
  phase = 'login_session';
  const cookie = (await context.cookies(origin)).find((item) => item.name === 'lab_admin_session');
  assert.ok(cookie?.httpOnly && cookie.sameSite === 'Lax' && !cookie.value.includes('.'));
  assert.equal(await page.evaluate(() => document.cookie), '');
  const response = await page.request.get(origin + '/api/session');
  const value = await response.json();
  assert.equal(value.user.subject, name);
  assert.ok(!value.access_token && !value.id_token && !value.token);
  return value;
}
async function logout() {
  await page.locator('#logout').click();
  await page.locator('#login').waitFor({state: 'visible'});
  assert.equal((await page.request.get(origin + '/api/config')).status(), 401);
}
try {
  browser = await chromium.launch({headless: true});
  report.browser = {name: 'Chromium', version: browser.version(), playwright: (await import('playwright/package.json', {with: {type: 'json'}})).default.version};
  context = await browser.newContext({viewport: {width: 1280, height: 900}});
  page = await context.newPage();
  page.on('request', observeViewRequest);
  page.on('response', (response) => { void observeViewResponse(response); });
  const pageErrors = [];
  page.on('pageerror', () => pageErrors.push('uncaught_application_error'));
  phase = 'alpha_login';
  // Hold one actual successful response, without replacing its body or status.
  // Visibility must mean the initial view is coherent, rather than an empty pill.
  const configGate = new Promise((done) => { releaseConfig = done; });
  await page.route(origin + '/api/config', async (route) => {
    const response = await route.fetch();
    const body = await response.body();
    assert.equal(response.status(), 200);
    configReceived = true;
    await configGate;
    await route.fulfill({response, body});
  }, {times: 1});
  await login('admin-alpha');
  record('Initial desk stays hidden until the real configuration response and initial view load complete; controlled response delay');
  record('Actual Code+PKCE login uses a user account and an HttpOnly opaque cookie');
  phase = 'keyboard_update';
  const before = await (await page.request.get(origin + '/api/config')).json();
  await page.locator('#page-size').fill('2');
  await page.locator('#status').selectOption('closed');
  await page.locator('#prepare').focus();
  await page.keyboard.press('Enter');
  await page.locator('#confirmation').waitFor({state: 'visible'});
  assert.equal(await page.locator('#commit').evaluate((element) => element === document.activeElement), true);
  assert.equal((await (await page.request.get(origin + '/api/config')).json()).revision, before.revision);
  const command = JSON.parse(await page.locator('#pending-command').textContent());
  assert.equal(command.expected_revision, before.revision);
  await page.keyboard.press('Enter');
  await until(async () => !(await page.locator('#receipt').isHidden())
    && JSON.parse(await page.locator('#receipt').textContent()).revision === before.revision + 1
    && await page.locator('#prepare').isEnabled());
  const after = await (await page.request.get(origin + '/api/config')).json();
  assert.deepEqual(after.config, {work_order_page_size: 2, default_status: 'closed'});
  record('Keyboard review and confirmation change the real configured view only after confirmation');
  phase = 'rollback';
  await page.locator('#rollback-target').fill('0');
  await page.locator('#prepare-rollback').click();
  await page.locator('#commit').click();
  await until(async () => JSON.parse(await page.locator('#receipt').textContent()).revision === before.revision + 2
    && await page.locator('#prepare').isEnabled());
  assert.deepEqual((await (await page.request.get(origin + '/api/config')).json()).config,
    {work_order_page_size: 3, default_status: 'open'});
  record('Rollback appends a revision and the configuration audit remains visible');
  phase = 'saved_command_recovery';
  await page.locator('#page-size').fill('3');
  await page.locator('#prepare').click();
  const saved = await page.locator('#pending-command').textContent();
  await page.reload();
  await page.locator('#retry').waitFor({state: 'visible'});
  assert.equal(await page.locator('#pending-command').textContent(), saved);
  assert.equal((await (await page.request.get(origin + '/api/config')).json()).revision, before.revision + 2);
  await page.locator('#cancel').click();
  record('Tab reload preserves only the account-bound immutable command without automatically submitting it');
  phase = 'desktop_pixels';
  mkdirSync(dirname(output), {recursive: true});
  await page.screenshot({path: join(dirname(output), 'admin-desktop.png'), fullPage: true});
  await logout();
  phase = 'auditor';
  const auditor = await login('auditor-alpha');
  phase = 'auditor';
  assert.equal(auditor.can_configure, false);
  assert.equal(await page.locator('#prepare').isDisabled(), true);
  assert.equal(await page.locator('#read-only').isVisible(), true);
  assert.match(await page.locator('#audit').textContent(), /admin-alpha/);
  record('Auditor inspects this tenant and audit while mutation controls stay disabled');
  await logout();
  phase = 'beta';
  await login('admin-beta');
  phase = 'beta';
  assert.match(await page.locator('#identity').textContent(), /tenant beta/);
  const beta = await (await page.request.get(origin + '/api/work-orders')).json();
  assert.ok(beta.items.every((row) => row.tenant_id === 'beta'));
  const audit = await (await page.request.get(origin + '/api/audit')).json();
  assert.ok(audit.events.every((row) => row.tenant_id === 'beta'));
  assert.equal((await page.request.get(origin + '/api/config?tenant_id=alpha')).status(), 400);
  record('Beta work orders and audit remain tenant-local; a query-string tenant is rejected');
  phase = 'mobile';
  await page.setViewportSize({width: 390, height: 844});
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth + 1), false);
  await page.screenshot({path: join(dirname(output), 'admin-mobile.png'), fullPage: true});
  record('The rendered desk stays within a 390-pixel viewport', {viewport_width: 390});
  assert.deepEqual(pageErrors, []);
  record('No uncaught application errors in the actual browser');
  await logout();
  report.passed = true;
  report.screenshots = ['admin-desktop.png', 'admin-mobile.png'];
} catch {
  report.failure_stage = phase;
  report.failure_context = failureViewContext();
  viewActive = false;
  report.failure = 'Actual browser assertion failed; raw URLs, credentials and exception text are omitted.';
  process.exitCode = 1;
} finally {
  releaseConfig();
  accounts.clear();
  await context?.close();
  await browser?.close();
  mkdirSync(dirname(output), {recursive: true});
  writeFileSync(output, JSON.stringify(report, null, 2) + '\n', {mode: 0o600, flag: 'wx'});
}
