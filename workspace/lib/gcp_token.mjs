// Sign a Google service-account JWT (RS256) for operator_config.py.
//
// RS256 is RSASSA-PKCS1-v1_5 over SHA-256, which the Python standard library
// cannot produce and the image has no package to supply. Node's built-in
// node:crypto can, and Node is already in the image, so this one step lives here.
//
// It only signs. It opens no socket and exchanges nothing: the token request,
// caching and every Firestore call stay in Python. That keeps the part holding
// a private key small enough to read in one sitting, and testable offline,
// which the --network none acceptance job requires.
//
// Input arrives on stdin as JSON {header, claims, private_key}; the key is never
// accepted on argv, where any user on the box could read it from `ps`. Output is
// the compact JWT and a newline. Nothing written to stderr ever includes the key.

import crypto from 'node:crypto';
import { pathToFileURL } from 'node:url';

function base64url(value) {
  return Buffer.from(JSON.stringify(value)).toString('base64url');
}

export function signJwt({ header, claims, private_key: privateKey }) {
  if (!header || header.alg !== 'RS256' || header.typ !== 'JWT') {
    throw new Error('header must be {alg: "RS256", typ: "JWT"}');
  }
  if (!claims || typeof claims !== 'object') throw new Error('claims must be an object');
  if (typeof privateKey !== 'string' || !privateKey.includes('PRIVATE KEY')) {
    throw new Error('private_key must be a PEM private key');
  }
  const signingInput = `${base64url(header)}.${base64url(claims)}`;
  // For an RSA key node:crypto.sign defaults to PKCS#1 v1.5 padding, which is
  // exactly what RS256 means.
  const signature = crypto.sign('sha256', Buffer.from(signingInput), privateKey);
  return `${signingInput}.${signature.toString('base64url')}`;
}

async function main() {
  const chunks = [];
  for await (const chunk of process.stdin) chunks.push(chunk);
  let input;
  try {
    input = JSON.parse(Buffer.concat(chunks).toString('utf8'));
  } catch {
    process.stderr.write('gcp_token: stdin is not JSON\n');
    process.exit(2);
  }
  try {
    process.stdout.write(`${signJwt(input)}\n`);
  } catch (error) {
    // The message comes from our own checks or from OpenSSL's key parser,
    // neither of which echoes key material.
    process.stderr.write(`gcp_token: ${error.message}\n`);
    process.exit(2);
  }
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  await main();
}
