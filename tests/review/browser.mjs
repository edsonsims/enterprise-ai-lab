/** Actual Chromium + local OIDC + review UI. No API mocks, tracing or HARs. */
import assert from 'node:assert/strict';
import { constants, openSync, fstatSync, readFileSync, closeSync, mkdirSync, writeFileSync } from 'node:fs';
import { createHash, randomUUID } from 'node:crypto';
import { dirname, join, resolve } from 'node:path';
import { chromium } from 'playwright';

const [portalArgument, accountsArgument, outputArgument] = process.argv.slice(2);
if (!portalArgument || !accountsArgument || !outputArgument || process.argv.length !== 5) {
  throw new Error('Usage: node tests/review/browser.mjs PORTAL_URL PRIVATE_ACCOUNTS_FILE OUT_JSON');
}
const portal = new URL(portalArgument);
if (portal.protocol !== 'http:' || portal.hostname !== '127.0.0.1' || !portal.port
    || portal.username || portal.password || portal.search || portal.hash || portal.pathname !== '/') {
  throw new Error('Use the exact literal loopback review portal origin.');
}
const origin = portal.origin;
const output = resolve(outputArgument);
const mainId = process.env.REVIEW_BROWSER_MAIN || 'browser-main';
const rejectId = process.env.REVIEW_BROWSER_REJECT || 'browser-reject';
const betaId = process.env.REVIEW_BROWSER_BETA || 'browser-beta';
for (const id of [mainId, rejectId, betaId]) {
  assert.match(id, /^[A-Za-z0-9][A-Za-z0-9_.:-]{0,95}$/);
}

// Fixture credentials stay in the process and form fields. They never enter a
// report, screenshot, console message, trace, HAR, environment variable or URL.
function loadAccounts(path) {
  const fd = openSync(path, constants.O_RDONLY | constants.O_NOFOLLOW | constants.O_NONBLOCK);
  try {
    const stat = fstatSync(fd);
    assert.ok(stat.isFile() && stat.size <= 32768 && (stat.mode & 0o777) === 0o600);
    if (process.getuid) assert.equal(stat.uid, process.getuid());
    const source = readFileSync(fd);
    assert.ok(source.byteLength <= 32768);
    const value = JSON.parse(new TextDecoder('utf-8', { fatal: true }).decode(source));
    assert.equal(value.schema_version, 1);
    assert.ok(Array.isArray(value.accounts));
    const result = new Map();
    for (const entry of value.accounts) {
      assert.equal(typeof entry.username, 'string');
      assert.match(entry.password, /^[\x20-\x7e]{16,128}$/);
      assert.ok(!result.has(entry.username));
      result.set(entry.username, entry.password);
    }
    for (const username of ['reviewer-alpha', 'analyst-reviewer-alpha', 'reviewer-beta']) {
      assert.ok(result.has(username), 'Required local fixture account is missing.');
    }
    return result;
  } finally { closeSync(fd); }
}
const accounts = loadAccounts(accountsArgument);
const checks = [];
const report = {
  schema_version: 1,
  recorded_at: new Date().toISOString(),
  experiment: 'Actual Chromium browser review over local OIDC, Temporal and approved RAG publication',
  identities: 'Generated local fixture accounts; all browser actions are automated, not owner human review.',
  limitations: [
    'This run checks the actual browser and local services, not production identity or accessibility certification.',
    'PDF page-count headers are checked here; Python integration tests independently parse the returned single-page PDFs.',
    'No neural answer-quality or human reading/comprehension claim is made.',
  ],
  checks,
};
mkdirSync(dirname(output), { recursive: true });
let browser;
let context;
let page;
let phase = 'launch';

function record(name, detail = {}) {
  checks.push({ name, passed: true, ...detail });
}

async function until(operation, { timeout = 30000, interval = 150, message = 'Condition did not become true.' } = {}) {
  const deadline = Date.now() + timeout;
  while (Date.now() < deadline) {
    if (await operation()) return;
    await new Promise((resolvePromise) => setTimeout(resolvePromise, interval));
  }
  throw new Error(message);
}

async function login(username) {
  const loginPhase = phase;
  phase = `${loginPhase}_entry_navigation`;
  await page.goto(origin + '/', { waitUntil: 'domcontentloaded' });
  phase = `${loginPhase}_login_link`;
  await page.getByTestId('login-link').click();
  phase = `${loginPhase}_form_wait`;
  await page.locator('input[name="username"]').waitFor({ state: 'visible' });
  phase = `${loginPhase}_form_fill`;
  await page.locator('input[name="username"]').fill(username);
  await page.locator('input[name="password"]').fill(accounts.get(username));
  phase = `${loginPhase}_form_submit`;
  await page.getByRole('button', { name: 'Sign in', exact: true }).click();
  phase = `${loginPhase}_callback_wait`;
  await until(async () => {
    if (page.url().startsWith(origin + '/') && await page.getByTestId('session-identity').isVisible()) return true;
    const consent = page.getByRole('button', { name: 'Continue', exact: true });
    if (await consent.isVisible()) await consent.click();
    return false;
  }, { message: 'Actual local OIDC sign-in did not complete.' });
  phase = `${loginPhase}_identity_visible`;
  assert.equal(await page.getByTestId('session-identity').textContent(), username);
  phase = `${loginPhase}_session_cookie`;
  const cookies = await context.cookies(origin);
  const session = cookies.find((cookie) => cookie.name === 'lab_review_session');
  assert.ok(session?.httpOnly);
  assert.equal(session.sameSite, 'Lax');
  assert.ok(!session.value.includes('.'));
  phase = `${loginPhase}_browser_storage`;
  const publicState = await page.evaluate(() => ({
    localKeys: Object.keys(localStorage), sessionKeys: Object.keys(sessionStorage), visibleCookie: document.cookie,
  }));
  assert.deepEqual(publicState.localKeys, []);
  assert.deepEqual(publicState.sessionKeys, []);
  assert.equal(publicState.visibleCookie, '');
  phase = `${loginPhase}_session_json`;
  const sessionResponse = await page.request.get(origin + '/api/session');
  assert.equal(sessionResponse.status(), 200);
  const state = await sessionResponse.json();
  assert.ok(!Object.hasOwn(state, 'token') && !Object.hasOwn(state, 'access_token') && !Object.hasOwn(state, 'id_token'));
  assert.equal(state.user.subject, username);
  return state;
}

async function logout() {
  const cookie = (await context.cookies(origin)).find((value) => value.name === 'lab_review_session');
  assert.ok(cookie);
  await page.getByTestId('logout-button').click();
  await page.getByTestId('login-link').waitFor({ state: 'visible' });
  assert.equal(await page.locator('#review-list').textContent(), '');
  assert.equal(await page.locator('#audit-list').textContent(), '');
  assert.equal(await page.getByTestId('page-preview').count(), 0);
  const absent = await page.request.get(origin + '/api/session');
  assert.equal(absent.status(), 401);
  const replay = await page.request.get(origin + '/api/session', {
    headers: { Cookie: `${cookie.name}=${cookie.value}` },
  });
  assert.equal(replay.status(), 401);
}

async function openReview(id) {
  await page.getByTestId(`review-card-${id}`).click();
  await until(async () => await page.getByTestId('review-heading').isVisible()
    && await page.getByTestId('review-heading').textContent() === id);
  await page.getByTestId('chunk-list').waitFor({ state: 'visible' });
}

async function preview() {
  const image = page.getByTestId('page-preview');
  await image.waitFor({ state: 'visible' });
  await until(async () => image.evaluate((value) => value.complete && value.naturalWidth > 0), {
    message: 'The actual authorized PDF page pixels did not load.',
  });
  const src = await image.getAttribute('src');
  assert.match(src, /^\/api\/reviews\/.+\/preview\.png(?:\?|$)/);
  const pixels = await page.request.get(new URL(src, origin).href);
  assert.equal(pixels.status(), 200);
  assert.equal(pixels.headers()['content-type'], 'image/png');
  assert.equal(pixels.headers()['cache-control'], 'no-store');
  const png = await pixels.body();
  assert.ok(png.subarray(0, 8).equals(Buffer.from([137, 80, 78, 71, 13, 10, 26, 10])));
  assert.ok(png.byteLength >= 24);
  const width = png.readUInt32BE(16), height = png.readUInt32BE(20);
  assert.ok(width >= 100 && height >= 100 && width <= 4096 && height <= 4096);
  const decoded = await image.evaluate((value) => ({ width: value.naturalWidth, height: value.naturalHeight }));
  assert.deepEqual(decoded, { width, height });
  const href = await page.getByTestId('pdf-open').getAttribute('href');
  assert.match(href, /^\/api\/reviews\//);
  assert.ok(href.endsWith('#page=1&view=FitH'));
  const response = await page.request.get(new URL(href, origin).href.split('#')[0]);
  assert.equal(response.status(), 200);
  assert.equal(response.headers()['x-preview-page-count'], '1');
  assert.equal(response.headers()['content-type'], 'application/pdf');
  assert.equal(response.headers()['cache-control'], 'no-store');
  const bytes = await response.body();
  assert.ok(bytes.subarray(0, 5).equals(Buffer.from('%PDF-')));
  assert.match(response.headers()['x-preview-sha256'], /^[a-f0-9]{64}$/);
  return { pdf_bytes: bytes.byteLength, png_bytes: png.byteLength, decoded_width: width, decoded_height: height };
}

async function waitPublished(id) {
  await until(async () => {
    const response = page.waitForResponse((value) => {
      const url = new URL(value.url());
      return url.origin === origin && url.pathname === `/api/reviews/${encodeURIComponent(id)}`
        && value.request().method() === 'GET';
    });
    await page.getByTestId('review-refresh').click();
    assert.equal((await response).status(), 200);
    const status = await page.getByTestId('decision-status').textContent();
    return status.includes('published and available');
  }, { timeout: 60000, interval: 300, message: `Actual publication did not finish for ${id}.` });
  assert.match(await page.getByTestId(`review-card-${id}`).textContent(), /Publication: published/);
}

try {
  browser = await chromium.launch({ headless: true });
  report.browser = { name: 'Chromium', version: browser.version(), playwright: '1.62.1' };
  context = await browser.newContext({ viewport: { width: 1440, height: 1080 }, reducedMotion: 'reduce' });
  page = await context.newPage();
  page.setDefaultTimeout(15000);
  const failures = [];
  page.on('pageerror', () => { failures.push('uncaught browser application error'); });

  phase = 'reader_login';
  await login('reviewer-alpha');
  phase = 'partial_batch_review';
  await openReview(mainId);
  assert.equal(await page.getByTestId('document-select').locator('option').count(), 1);
  assert.match(await page.getByTestId('denied-documents').textContent(), /1 document is outside/);
  assert.ok(await page.getByTestId('approve-button').isDisabled());
  assert.ok(await page.getByTestId('reject-button').isDisabled());
  const readerBytes = await preview();
  assert.match(await page.getByTestId('chunk-list').textContent(), /per-session/);
  const ocrId = 'pdf-' + createHash('sha256').update(JSON.stringify(['alpha', 'nist-ai-rmf', 25])).digest('hex').slice(0, 40);
  const deniedPdf = await page.request.get(`${origin}/api/reviews/${encodeURIComponent(mainId)}/documents/${ocrId}/pdf`);
  assert.equal(deniedPdf.status(), 404);
  record('Reader-scoped reviewer sees one native page, cannot inspect OCR or decide an unreadable batch', {
    permitted_pdf_http: 200, forbidden_ocr_pdf_http: 404, preview_header_page_count: 1, actual_page_preview: readerBytes,
  });
  record('HttpOnly opaque session; no tokens in session JSON, JavaScript cookies or browser storage');
  phase = 'reader_logout';
  await logout();
  record('Logout clears protected UI and invalidates the previous server session', { session_http: 401, old_cookie_replay_http: 401 });

  phase = 'analyst_login';
  await login('analyst-reviewer-alpha');
  phase = 'native_and_ocr_review';
  await openReview(mainId);
  assert.equal(await page.getByTestId('document-select').locator('option').count(), 2);
  assert.equal(await page.getByTestId('denied-documents').count(), 0);
  assert.ok(await page.getByTestId('approve-button').isEnabled());
  const nativeBytes = await preview();
  assert.match(await page.getByTestId('source-details').textContent(), /Original page15/);
  assert.match(await page.getByTestId('chunk-list').textContent(), /per-session/);
  await page.getByTestId('document-select').selectOption('1');
  const ocrBytes = await preview();
  assert.match(await page.getByTestId('source-details').textContent(), /Original page25/);
  assert.match(await page.getByTestId('source-details').textContent(), /Artifact page1/);
  assert.match(await page.getByTestId('source-details').textContent(), /OCR/);
  assert.match(await page.getByTestId('chunk-list').textContent(), /GOVERN|MEASURE|MANAGE/);
  const digest = await page.getByTestId('snapshot-digest').textContent();
  assert.match(digest, /^[a-f0-9]{64}$/);
  phase = 'csrf_boundary';
  const noCsrf = await page.request.post(`${origin}/api/reviews/${encodeURIComponent(mainId)}/decisions`, {
    headers: { Origin: origin }, data: { command_id: `missing-csrf-${randomUUID()}`, snapshot_sha256: digest, decision: 'approve' },
  });
  assert.equal(noCsrf.status(), 403);
  record('Analyst reviewer sees actual PDFium PNG page pixels, native/OCR PDF bytes, source hashes and exact chunk spans', {
    authorized_documents: 2, native_preview: nativeBytes, ocr_preview: ocrBytes,
  });
  record('A mutation without the session CSRF token is refused', { http: 403 });
  phase = 'approval_and_publication';
  await page.getByTestId('approve-button').click();
  await waitPublished(mainId);
  assert.ok(await page.getByTestId('approve-button').isDisabled());
  assert.ok(await page.getByTestId('reject-button').isDisabled());
  await page.locator('#app-message').waitFor({ state: 'hidden' });
  await page.screenshot({ path: join(dirname(output), 'browser-review.png'), fullPage: true });
  record('Approval through the actual UI produces a durable receipt and a separate completed publication');

  phase = 'rejection';
  await openReview(rejectId);
  assert.ok(await page.getByTestId('reject-button').isEnabled());
  await page.getByTestId('reject-button').click();
  await until(async () => (await page.getByTestId('decision-status').textContent()).includes('Rejection recorded'));
  assert.ok(await page.getByTestId('approve-button').isDisabled());
  record('Rejection through the actual UI records a terminal rejection without authorizing publication');

  phase = 'audit_ui';
  await page.getByRole('tab', { name: 'Audit trail', exact: true }).click();
  await page.getByTestId('audit-integrity').waitFor({ state: 'visible' });
  assert.match(await page.getByTestId('audit-integrity').textContent(), /Local chain verified/);
  const audit = await page.getByTestId('audit-list').textContent();
  assert.match(audit, /decision accepted/);
  assert.match(audit, /publication observed/);
  assert.match(audit, /analyst-reviewer-alpha/);
  record('Audit UI distinguishes an accepted decision from an observed publication and verifies the local chain');
  await logout();

  phase = 'beta_login';
  await login('reviewer-beta');
  phase = 'beta_isolation_and_publication';
  await openReview(betaId);
  assert.equal(await page.getByTestId(`review-card-${mainId}`).count(), 0);
  assert.equal(await page.getByTestId(`review-card-${rejectId}`).count(), 0);
  const alphaDetail = await page.request.get(`${origin}/api/reviews/${encodeURIComponent(mainId)}`);
  assert.equal(alphaDetail.status(), 404);
  await preview();
  await page.getByTestId('approve-button').click();
  await waitPublished(betaId);
  record('Beta reviewer sees and approves only the Beta queue; direct Alpha review access is denied', { alpha_detail_http: 404 });

  phase = 'mobile_layout';
  await page.setViewportSize({ width: 390, height: 844 });
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth + 1);
  assert.equal(overflow, false);
  await page.locator('#app-message').waitFor({ state: 'hidden' });
  await page.screenshot({ path: join(dirname(output), 'browser-review-mobile.png'), fullPage: true });
  record('Review desk remains within a 390-pixel viewport', { viewport_width: 390 });
  assert.deepEqual(failures, []);
  record('No uncaught browser application errors');
  phase = 'final_logout';
  await logout();
  report.passed = true;
  report.screenshots = ['browser-review.png', 'browser-review-mobile.png'];
  writeFileSync(output, JSON.stringify(report, null, 2) + '\n', { mode: 0o600 });
  console.log(JSON.stringify({ event: 'browser_verified', passed_checks: checks.length, browser: report.browser.name, output: 'safe browser report written' }));
} catch (error) {
  // Browser exceptions may include a URL with a code/state query or a locator
  // containing a supplied credential. Keep those details out of artifacts.
  report.passed = false;
  report.failure_stage = phase;
  report.failure_locations = [...new Set((typeof error?.stack === 'string' ? error.stack : '')
    .match(/browser\.mjs:\d+:\d+/g) || [])];
  report.failure = 'An actual browser assertion failed; inspect the local services and rerun without publishing private logs.';
  writeFileSync(output, JSON.stringify(report, null, 2) + '\n', { mode: 0o600 });
  console.error('Actual browser review verification failed; safe partial report written.');
  process.exitCode = 1;
} finally {
  accounts.clear();
  await context?.close();
  await browser?.close();
}
