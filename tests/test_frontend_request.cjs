const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const vm = require('node:vm');
const { test } = require('node:test');
const source = readFileSync('tools/sx1302/superlink/bridge/frontend/request.js', 'utf8');
function request(status, body) {
  const context = vm.createContext({
    AbortSignal,
    fetch: async () => new Response(body, { status }),
  });
  vm.runInContext(source, context);
  return vm.runInContext('requestJSON("api/state")', context);
}
test('plain-text 502 and HTML 503 become useful errors', async () => {
  for (const [status, body] of [[502, '502: Bad Gateway'], [503, '<html>Unavailable</html>']]) {
    await assert.rejects(request(status, body), error =>
      error.status === status && error.message.includes(`HTTP ${status}`) && !error.message.includes('JSON'));
  }
});
test('401 without JSON still preserves status for locking', async () => {
  await assert.rejects(request(401, '401: Unauthorized'), error => error.status === 401 && /Access denied/.test(error.message));
});
test('successful invalid/empty/scalar responses fail clearly', async () => {
  for (const body of ['', 'null', '42', '[]', '<html>Login</html>'])
    await assert.rejects(request(200, body), /Unexpected response/);
});
test('JSON errors and successful objects retain meaning', async () => {
  await assert.rejects(request(400, '{"error":"Device already exists"}'), /Device already exists/);
  assert.equal((await request(200, '{"ingress":true}')).ingress, true);
});
test('network errors and timeouts are readable', async () => {
  const context = vm.createContext({ AbortSignal, fetch: async () => { throw Error('Failed to fetch'); } });
  vm.runInContext(source, context);
  await assert.rejects(vm.runInContext('requestJSON("api/state")', context), /Cannot reach the bridge/);
});
