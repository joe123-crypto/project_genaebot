import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import crypto from 'node:crypto';
import test from 'node:test';
import { fileURLToPath } from 'node:url';
import { signJwt } from './gcp_token.mjs';

const SIGNER = fileURLToPath(new URL('./gcp_token.mjs', import.meta.url));
const HEADER = { alg: 'RS256', typ: 'JWT' };
const CLAIMS = {
  iss: 'runtime@example.iam.gserviceaccount.com',
  scope: 'https://www.googleapis.com/auth/datastore',
  aud: 'https://oauth2.googleapis.com/token',
  iat: 1_800_000_000,
  exp: 1_800_003_600,
};

function keyPair() {
  return crypto.generateKeyPairSync('rsa', {
    modulusLength: 2048,
    publicKeyEncoding: { type: 'spki', format: 'pem' },
    privateKeyEncoding: { type: 'pkcs8', format: 'pem' },
  });
}

function verifies(jwt, publicKey) {
  const [header, claims, signature] = jwt.split('.');
  return crypto.verify('sha256', Buffer.from(`${header}.${claims}`), publicKey,
    Buffer.from(signature, 'base64url'));
}

test('signs a JWT that Google would accept: RS256 over header.claims', () => {
  const { publicKey, privateKey } = keyPair();
  const jwt = signJwt({ header: HEADER, claims: CLAIMS, private_key: privateKey });
  const [header, claims] = jwt.split('.').slice(0, 2)
    .map((part) => JSON.parse(Buffer.from(part, 'base64url').toString('utf8')));
  assert.deepEqual(header, HEADER);
  assert.deepEqual(claims, CLAIMS);
  assert.ok(verifies(jwt, publicKey));
});

test('a signature from a different key does not verify', () => {
  const signer = keyPair();
  const other = keyPair();
  const jwt = signJwt({ header: HEADER, claims: CLAIMS, private_key: signer.privateKey });
  assert.equal(verifies(jwt, other.publicKey), false);
});

test('refuses any algorithm but RS256, so a typo cannot downgrade the signature', () => {
  const { privateKey } = keyPair();
  assert.throws(() => signJwt({ header: { alg: 'none', typ: 'JWT' }, claims: CLAIMS,
    private_key: privateKey }), /RS256/);
  assert.throws(() => signJwt({ header: { alg: 'HS256', typ: 'JWT' }, claims: CLAIMS,
    private_key: privateKey }), /RS256/);
});

test('the CLI reads the key from stdin and prints only the JWT', () => {
  const { publicKey, privateKey } = keyPair();
  const run = spawnSync(process.execPath, [SIGNER], {
    input: JSON.stringify({ header: HEADER, claims: CLAIMS, private_key: privateKey }),
  });
  assert.equal(run.status, 0, run.stderr.toString());
  const jwt = run.stdout.toString().trim();
  assert.equal(jwt.split('.').length, 3);
  assert.ok(verifies(jwt, publicKey));
});

test('a malformed key fails without echoing it to stderr', () => {
  // A canary that looks like key material: if any of it reaches stderr, the
  // error path is leaking what it was given.
  const canary = '-----BEGIN PRIVATE KEY-----\nQ0FOQVJZLWtleS1tYXRlcmlhbA==\n-----END PRIVATE KEY-----\n';
  const run = spawnSync(process.execPath, [SIGNER], {
    input: JSON.stringify({ header: HEADER, claims: CLAIMS, private_key: canary }),
  });
  assert.equal(run.status, 2);
  assert.equal(run.stdout.toString(), '');
  assert.ok(!run.stderr.toString().includes('Q0FOQVJZ'), run.stderr.toString());
});

test('non-JSON stdin is a clean refusal', () => {
  const run = spawnSync(process.execPath, [SIGNER], { input: 'not json' });
  assert.equal(run.status, 2);
  assert.match(run.stderr.toString(), /not JSON/);
});
