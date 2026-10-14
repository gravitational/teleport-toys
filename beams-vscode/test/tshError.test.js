const assert = require('node:assert/strict');
const test = require('node:test');

const { classifyTshError, tshErrorMessage, stripAnsi } = require('../out/tsh');

const err = (message, extra = {}) => Object.assign(new Error(message), extra);

// Verbatim stderr from `tsh beams exec <gone-beam> -- ls`, ANSI escapes included.
const GONE_BEAM_STDERR =
    'Command failed: tsh beams exec gone -- ls\n' +
    '\u001b[31mERROR: \u001b[0mcannot relogin in non-interactive session\n' +
    '\t"beam" "gone" does not exist\n';

test('strips ANSI escapes and command noise from tsh error text', () => {
    const msg = tshErrorMessage(err(GONE_BEAM_STDERR));
    assert.ok(!msg.includes('\u001b'), 'ANSI escapes should be stripped');
    assert.ok(!msg.startsWith('Command failed:'), 'exec preamble should be stripped');
    assert.ok(!msg.includes('ERROR:'), 'ERROR: prefix should be stripped');
    assert.match(msg, /"beam" "gone" does not exist/);
    assert.equal(stripAnsi('\u001b[31mred\u001b[0m'), 'red');
});

// A gone beam makes tsh emit the relogin line *and* "does not exist"; it must classify as a
// disconnect, otherwise the user gets sent to `tsh login` to fix an expired sandbox.
test('classifies a gone beam as disconnected, not auth', () => {
    assert.equal(classifyTshError(err(GONE_BEAM_STDERR)), 'disconnected');
});

test('classifies transport failures as disconnected', () => {
    for (const message of [
        'ssh: connect to host beam port 22: Connection refused',
        'read: connection reset by peer',
        'context deadline exceeded',
        'dial tcp 10.0.0.1:3022: i/o timeout',
        'ssh: handshake failed: EOF',
        'write: broken pipe',
    ]) {
        assert.equal(classifyTshError(err(message)), 'disconnected', message);
    }
});

test('classifies an expired or missing login as auth', () => {
    for (const message of [
        'ERROR: cannot relogin in non-interactive session',
        'ERROR: access denied to user beams',
        'x509: certificate has expired or is not yet valid',
        'not logged in',
    ]) {
        assert.equal(classifyTshError(err(message)), 'auth', message);
    }
});

test('treats a killed or timed-out child process as disconnected', () => {
    assert.equal(classifyTshError(err('Command failed', { killed: true })), 'disconnected');
    assert.equal(classifyTshError(err('timeout', { code: 'ETIMEDOUT' })), 'disconnected');
});

// Real command failures must stay errors — misclassifying them as a disconnect would
// silently downgrade actionable git/tooling problems to an informational popup.
test('leaves genuine command failures as other', () => {
    for (const message of [
        'fatal: could not read Username for https://github.com',
        'nothing to commit, working tree clean',
        'error: pathspec did not match any file(s) known to git',
    ]) {
        assert.equal(classifyTshError(err(message)), 'other', message);
    }
});

test('explains a silent tsh failure instead of returning an empty message', () => {
    const timedOut = Object.assign(new Error('Command failed: tsh beams add -f json\n'), { killed: true, signal: 'SIGTERM' });
    assert.equal(tshErrorMessage(timedOut), 'tsh timed out (SIGTERM)');
    const exited = Object.assign(new Error('Command failed: tsh beams add -f json\n'), { code: 1 });
    assert.equal(tshErrorMessage(exited), 'tsh exited with code 1 without an error message');
});
