'use strict';
const $ = (id) => document.getElementById(id);
let session = null;
let snapshot = null;
let pending = null;
let uncertain = null;
const commandKey = 'admin_immutable_command';
const tell = (text) => { $('message').textContent = text; };
function saveCommand(command) {
  // Configuration recovery data is untrusted, tenant/account-bound tab state.
  // Bearer tokens, CSRF values and passwords never enter browser storage.
  sessionStorage.setItem(commandKey, JSON.stringify({subject: session.user.subject, tenant_id: session.user.tenant_id, command}));
}
function restoreCommand() {
  try {
    const value = JSON.parse(sessionStorage.getItem(commandKey) || 'null');
    if (!value || value.subject !== session.user.subject || value.tenant_id !== session.user.tenant_id) return;
    uncertain = value.command;
    $('pending-command').textContent = JSON.stringify(uncertain, null, 2);
    $('change-summary').textContent = 'Saved command for this signed account. Inspect refreshed durable state, then retry only explicitly.';
    $('confirmation').hidden = false;
    $('commit').hidden = true;
    $('retry').hidden = false;
    tell('An immutable command was recovered from this tab. Nothing was automatically retried.');
  } catch { sessionStorage.removeItem(commandKey); }
}
async function api(path, body) {
  const options = {cache: 'no-store', credentials: 'same-origin'};
  if (body !== undefined) {
    options.method = 'POST';
    options.headers = {'Content-Type': 'application/json', 'X-CSRF-Token': session?.csrf_token || ''};
    options.body = JSON.stringify(body);
  }
  const response = await fetch(path, options);
  const value = await response.json();
  if (!response.ok) {
    const failure = new Error(value.error?.message || 'State could not be confirmed.');
    failure.status = response.status;
    throw failure;
  }
  return value;
}
function list(target, values, render) {
  target.replaceChildren();
  for (const value of values) { const item = document.createElement('li'); item.textContent = render(value); target.append(item); }
}
async function refresh() {
  snapshot = await api('/api/config');
  $('revision').textContent = `Revision ${snapshot.revision}`;
  $('page-size').value = snapshot.config.work_order_page_size;
  $('status').value = snapshot.config.default_status;
  const orders = await api('/api/work-orders');
  list($('work-orders'), orders.items, (row) => `${row.work_order_id} · ${row.title} · ${row.status}`);
  $('page-note').textContent = orders.next_after ? 'More permitted rows exist. This milestone displays the configured first page.' : 'End of the permitted first page.';
  if (session.can_audit) {
    const audit = await api('/api/audit');
    $('audit').replaceChildren();
    for (const event of audit.events) {
      const item = document.createElement('li');
      const title = document.createElement('strong');
      title.textContent = `Revision ${event.revision} · ${event.action} · ${event.subject}`;
      const command = document.createElement('p');
      command.textContent = `Command ${event.command_id}`;
      const details = document.createElement('details');
      const summary = document.createElement('summary');
      summary.textContent = 'Inspect mutation hashes';
      const metadata = document.createElement('pre');
      metadata.textContent = JSON.stringify(event, null, 2);
      details.append(summary, metadata); item.append(title, command, details); $('audit').append(item);
    }
  } else $('audit').replaceChildren();
}
function confirmation(command, description) {
  pending = command;
  saveCommand(command);
  $('pending-command').textContent = JSON.stringify(command, null, 2);
  $('change-summary').textContent = description;
  $('confirmation').hidden = false;
  $('commit').hidden = false;
  $('commit').focus();
}
async function execute(command) {
  let committedReceipt = null;
  $('commit').disabled = true;
  $('retry').disabled = true;
  for (const id of ['cancel', 'prepare', 'prepare-rollback']) $(id).disabled = true;
  try {
    const receipt = await api('/api/commands', command);
    committedReceipt = receipt;
    $('receipt').textContent = JSON.stringify(receipt, null, 2);
    $('receipt').hidden = $('receipt-label').hidden = false;
    pending = uncertain = null;
    sessionStorage.removeItem(commandKey);
    $('confirmation').hidden = $('retry').hidden = true;
    await refresh();
    tell(`Committed revision ${receipt.revision}. The receipt and audit survived in local SQLite.`);
  } catch (failure) {
    if (committedReceipt) {
      tell(`Receipt confirmed revision ${committedReceipt.revision}. Refreshing the view failed; inspect durable state explicitly. The command was not retried.`);
      return;
    }
    // Keep the exact immutable command in this tab after an uncertain response.
    // No hidden retry, changed ID, or implied rollback.
    uncertain = command;
    $('retry').hidden = false;
    tell(`${failure.message} The exact command is retained in this tab. Refresh first; retry only explicitly.`);
  } finally {
    $('commit').disabled = $('retry').disabled = false;
    for (const id of ['cancel', 'prepare', 'prepare-rollback']) $(id).disabled = !session?.can_configure;
  }
}
$('config-form').addEventListener('submit', (event) => {
  event.preventDefault();
  if (!session?.can_configure || !snapshot) return;
  const size = Number($('page-size').value);
  if (!Number.isInteger(size) || size < 1 || size > 10) return;
  confirmation({command_id: crypto.randomUUID(), expected_revision: snapshot.revision, action: 'update',
    config: {work_order_page_size: size, default_status: $('status').value}},
  `Tenant ${session.user.tenant_id}: change page size from ${snapshot.config.work_order_page_size} to ${size}, status from ${snapshot.config.default_status} to ${$('status').value}. Expected revision ${snapshot.revision}.`);
});
$('rollback-form').addEventListener('submit', (event) => {
  event.preventDefault();
  const target = Number($('rollback-target').value);
  if (!session?.can_configure || !snapshot || !Number.isInteger(target) || target < 0 || target > 1000) return;
  confirmation({command_id: crypto.randomUUID(), expected_revision: snapshot.revision, action: 'rollback', target_revision: target},
    `Restore this tenant’s configuration from revision ${target}. This creates a new revision after ${snapshot.revision}; history is retained.`);
});
$('commit').addEventListener('click', () => { if (pending) execute(pending); });
$('retry').addEventListener('click', () => { if (uncertain) execute(uncertain); });
$('cancel').addEventListener('click', () => { pending = uncertain = null; sessionStorage.removeItem(commandKey); $('confirmation').hidden = $('retry').hidden = true; tell('Saved command discarded from this tab. Refresh durable state to inspect any committed change.'); $('prepare').focus(); });
$('refresh').addEventListener('click', () => refresh().then(() => tell('Durable state refreshed. No command was retried.')).catch((e) => tell(e.message)));
$('logout').addEventListener('click', async () => {
  try { await api('/logout', {}); pending = uncertain = null; sessionStorage.removeItem(commandKey); location.assign('/'); } catch (failure) { tell(failure.message); }
});
(async () => {
  try {
    session = await api('/api/session');
    $('identity').textContent = `${session.user.subject} · tenant ${session.user.tenant_id}`;
    $('login').hidden = $('signed-out').hidden = true;
    $('logout').hidden = false;
    if (!session.can_read) { tell('This signed account has no configuration-reading role and scope.'); return; }
    for (const id of ['page-size', 'status', 'prepare', 'rollback-target', 'prepare-rollback']) $(id).disabled = !session.can_configure;
    $('read-only').hidden = session.can_configure;
    await refresh();
    $('desk').hidden = false;
    if (session.can_configure) restoreCommand();
  } catch (failure) { if (failure.status !== 401) tell(failure.message); }
})();
